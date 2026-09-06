from arenamcp.server import get_draft_guidance


def test_draft_guidance_tool():
    # just ensuring it can be imported and executed in a stub environment
    assert callable(get_draft_guidance)
    assert getattr(get_draft_guidance, "name", get_draft_guidance.__name__) == "get_draft_guidance"
