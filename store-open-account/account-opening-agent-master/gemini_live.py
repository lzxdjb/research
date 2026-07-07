import asyncio
import inspect
import json
import logging
import traceback
from pathlib import Path

logger = logging.getLogger(__name__)
from google import genai
from google.genai import types


def _load_system_prompt(branch: str = "DOMESTIC") -> str:
    """Load system prompt by branch.

    Composition: common + <branch>.
    - common: `agent/gemini_system_prompt_common.md`
    - branch: `agent/gemini_system_prompt_<branch>.md` (lowercase branch name)

    Falls back to the legacy single-file `agent/gemini_system_prompt.md` if
    either piece is missing, so existing deployments keep working until the
    split files are present on disk.
    """
    branch_norm = (branch or "DOMESTIC").upper()
    if branch_norm not in ("DOMESTIC", "FOREIGNER"):
        logger.warning(f"Unknown branch '{branch}', falling back to DOMESTIC prompt")
        branch_norm = "DOMESTIC"

    agent_dir = Path(__file__).parent / "agent"
    common_path = agent_dir / "gemini_system_prompt_common.md"
    branch_path = agent_dir / f"gemini_system_prompt_{branch_norm.lower()}.md"
    legacy_path = agent_dir / "gemini_system_prompt.md"

    if common_path.exists() and branch_path.exists():
        common_text = common_path.read_text(encoding="utf-8")
        branch_text = branch_path.read_text(encoding="utf-8")
        merged = f"{common_text}\n\n---\n\n{branch_text}"
        logger.info(
            f"Loaded prompt: common + {branch_norm.lower()} "
            f"(common={len(common_text)} chars, branch={len(branch_text)} chars, total={len(merged)} chars)"
        )
        return merged

    if legacy_path.exists():
        logger.warning(
            f"Split prompt files missing (common={common_path.exists()}, "
            f"branch={branch_path.exists()}); falling back to legacy {legacy_path}"
        )
        return legacy_path.read_text(encoding="utf-8")

    logger.warning(f"No system prompt found in {agent_dir}, using fallback")
    return "You are a senior onboarding specialist at Light Horse."

class GeminiLive:
    """
    Handles the interaction with the Gemini Live API.
    """
    def __init__(self, api_key, model, input_sample_rate, tools=None, tool_mapping=None, branch: str = "DOMESTIC"):
        """
        Initializes the GeminiLive client.

        Args:
            api_key (str): The Gemini API Key.
            model (str): The model name to use.
            input_sample_rate (int): The sample rate for audio input.
            tools (list, optional): List of tools to enable. Defaults to None.
            tool_mapping (dict, optional): Mapping of tool names to functions. Defaults to None.
            branch (str, optional): Onboarding branch — 'DOMESTIC' or 'FOREIGNER'. Determines which
                system prompt is loaded at session start. Defaults to 'DOMESTIC'.
        """
        self.api_key = api_key
        self.model = model
        self.input_sample_rate = input_sample_rate
        self.client = genai.Client(api_key=api_key)
        self.tools = tools or []
        self.tool_mapping = tool_mapping or {}
        self.branch = (branch or "DOMESTIC").upper()

    async def start_session(self, audio_input_queue, video_input_queue, text_input_queue, audio_output_callback, audio_interrupt_callback=None):
        config = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name="Zephyr"
                    )
                ),
            ),
            system_instruction=types.Content(parts=[types.Part(text=_load_system_prompt(self.branch))]),
                        input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            realtime_input_config=types.RealtimeInputConfig(
                turn_coverage="TURN_INCLUDES_ONLY_ACTIVITY",
                automatic_activity_detection=types.AutomaticActivityDetection(
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                    prefix_padding_ms=200,
                    silence_duration_ms=500,
                ),
            ),
            tools=self.tools,
        )
        
        logger.info(f"Connecting to Gemini Live with model={self.model}")
        try:
          async with self.client.aio.live.connect(model=self.model, config=config) as session:
            logger.info("Gemini Live session opened successfully")
            
            session_resumed = False

            async def send_audio():
                try:
                    while True:
                        chunk = await audio_input_queue.get()
                        await session.send_realtime_input(
                            audio=types.Blob(data=chunk, mime_type=f"audio/pcm;rate={self.input_sample_rate}")
                        )
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.error(f"send_audio error: {e}\n{traceback.format_exc()}")

            async def send_video():
                try:
                    while True:
                        chunk = await video_input_queue.get()
                        await session.send_realtime_input(
                            video=types.Blob(data=chunk, mime_type="image/jpeg")
                        )
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.error(f"send_video error: {e}\n{traceback.format_exc()}")

            async def send_text():
                try:
                    while True:
                        text = await text_input_queue.get()
                        await session.send_realtime_input(text=text)
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.error(f"send_text error: {e}\n{traceback.format_exc()}")

            event_queue = asyncio.Queue()

            async def receive_loop():
                try:
                    while True:
                        async for response in session.receive():
                            if response.go_away:
                                logger.warning(f"Received GoAway from Gemini: {response.go_away}")
                                if not session_resumed:
                                    session_resumed = True
                                    # Reconnect and resume session
                                    await session.resume_session()
                                    logger.info("Session resumed after GoAway")
                            # if response.session_resumption_update:
                            #     logger.info(f"Session resumption update")
                            
                            server_content = response.server_content
                            tool_call = response.tool_call
                            
                            if server_content:
                                if server_content.model_turn:
                                    for part in server_content.model_turn.parts:
                                        if part.inline_data:
                                            mime_type = getattr(part.inline_data, "mime_type", "unknown")
                                            if mime_type and mime_type != "unknown":
                                                logger.info(f"Gemini audio output mime_type: {mime_type}")
                                            if inspect.iscoroutinefunction(audio_output_callback):
                                                await audio_output_callback(part.inline_data.data, mime_type)
                                            else:
                                                audio_output_callback(part.inline_data.data, mime_type)
                                
                                if server_content.input_transcription and server_content.input_transcription.text:
                                    await event_queue.put({"type": "user", "text": server_content.input_transcription.text})
                                
                                if server_content.output_transcription and server_content.output_transcription.text:
                                    await event_queue.put({"type": "gemini", "text": server_content.output_transcription.text})
                                
                                if server_content.turn_complete:
                                    await event_queue.put({"type": "turn_complete"})
                                
                                if server_content.interrupted:
                                    if audio_interrupt_callback:
                                        if inspect.iscoroutinefunction(audio_interrupt_callback):
                                            await audio_interrupt_callback()
                                        else:
                                            audio_interrupt_callback()
                                    await event_queue.put({"type": "interrupted"})

                            if tool_call:
                                function_responses = []
                                for fc in tool_call.function_calls:
                                    func_name = fc.name
                                    args = fc.args or {}

                                    if func_name not in self.tool_mapping:
                                        logger.warning(f"Tool '{func_name}' is not in tool_mapping, skipping")
                                        continue

                                    try:
                                        tool_func = self.tool_mapping[func_name]
                                        if inspect.iscoroutinefunction(tool_func):
                                            result = await tool_func(**args)
                                        else:
                                            loop = asyncio.get_running_loop()
                                            result = await loop.run_in_executor(None, lambda: tool_func(**args))
                                    except Exception as e:
                                        logger.error(f"Tool {func_name} error: {e}")
                                        result = f"Error: {e}"

                                    logger.info(f"Tool call: {func_name} | args: {json.dumps(args, ensure_ascii=False)[:300]}")
                                    logger.info(f"Tool result: {func_name} | {json.dumps(result, ensure_ascii=False)[:300]}")

                                    function_responses.append(types.FunctionResponse(
                                        name=func_name,
                                        id=fc.id,
                                        response={"result": result}
                                    ))
                                    await event_queue.put({"type": "tool_call", "name": func_name, "args": args, "result": result})

                                if function_responses:
                                    await session.send_tool_response(function_responses=function_responses)
                        
                        # session.receive() iterator ended — re-enter to keep listening

                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.error(f"receive_loop error: {type(e).__name__}: {e}\n{traceback.format_exc()}")
                    await event_queue.put({"type": "error", "error": f"{type(e).__name__}: {e}"})
                finally:
                    logger.info("receive_loop exiting")
                    await event_queue.put(None)

            send_audio_task = asyncio.create_task(send_audio())
            send_video_task = asyncio.create_task(send_video())
            send_text_task = asyncio.create_task(send_text())
            receive_task = asyncio.create_task(receive_loop())

            try:
                while True:
                    event = await event_queue.get()
                    if event is None:
                        break
                    if isinstance(event, dict) and event.get("type") == "error":
                        yield event
                        break
                    yield event
            finally:
                logger.info("Cleaning up Gemini Live session tasks")
                send_audio_task.cancel()
                send_video_task.cancel()
                send_text_task.cancel()
                receive_task.cancel()
        except Exception as e:
            logger.error(f"Gemini Live session error: {type(e).__name__}: {e}\n{traceback.format_exc()}")
            raise
        finally:
            logger.info("Gemini Live session closed")
