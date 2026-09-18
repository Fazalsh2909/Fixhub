"""Model resolution: FIXHUB_MODEL > NEXUS_MODEL alias > built-in default."""

from app.config import Settings


def test_default_model_when_nothing_set():
    s = Settings(fixhub_model="", nexus_model="", llm_provider="tokenrouter")
    assert s.resolved_model() == "z-ai/glm-5.3-free"


def test_nexus_model_alias_is_honored():
    s = Settings(
        fixhub_model="",
        nexus_model="minimax/minimax-m3:free",
        llm_provider="xkiro",
        xkiro_api_key="sk-test",
    )
    _, _, model = s.resolved_llm()
    assert model == "minimax/minimax-m3:free"


def test_fixhub_model_wins_over_alias():
    s = Settings(fixhub_model="gpt-4o-mini", nexus_model="minimax/minimax-m3:free")
    assert s.resolved_model() == "gpt-4o-mini"


def test_no_provider_prefix_mangling():
    """The model id already carries its vendor prefix — never prepend provider."""
    s = Settings(fixhub_model="minimax/minimax-m3:free", llm_provider="xkiro")
    _, _, model = s.resolved_llm()
    assert model == "minimax/minimax-m3:free"
    assert not model.startswith("xkiro/")
