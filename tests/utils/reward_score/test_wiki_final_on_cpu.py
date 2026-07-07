from verl.utils.reward_score import wiki_final


def test_wiki_final_scores_exact_match_on_cpu():
    result = wiki_final.compute_score(
        solution_str="<FINAL>\nAnswer: The United States\n</FINAL>",
        ground_truth="United States",
    )
    assert result["score"] == 1.0
    assert result["format_ok"] == 1.0


def test_wiki_final_penalizes_bad_format_on_cpu():
    result = wiki_final.compute_score(solution_str="Answer: United States", ground_truth="United States")
    assert result["score"] == -0.1
    assert result["format_ok"] == 0.0


def test_wiki_final_uses_aliases_from_meta_json_on_cpu():
    result = wiki_final.compute_score(
        solution_str="<FINAL>NYC</FINAL>",
        ground_truth="New York City",
        extra_info={"meta_json": '{"answer_aliases": ["NYC"]}'},
    )
    assert result["score"] == 1.0
