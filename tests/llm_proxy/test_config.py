"""Settings from the environment, and the daily limit's day change."""

import datetime

import pytest
from helpers import config

from app import limits  # pylint: disable=wrong-import-order


def test_vllm_names_still_configure_the_dev_stack():
    settings = config.load_settings(
        {
            "VLLM_BASE_URL": "http://vllm:8000/v1/",
            "VLLM_SERVED_MODEL_NAME": "aivi-8b-instruct",
        }
    )

    assert settings.upstream_base_url == "http://vllm:8000/v1"
    assert settings.model_alias == "aivi-8b-instruct"
    assert settings.upstream_model is None
    assert not settings.alias_mode
    assert settings.upstream_api_key == ""
    assert settings.daily_request_limit == 0


def test_upstream_names_win_over_vllm_names():
    settings = config.load_settings(
        {
            "VLLM_BASE_URL": "http://vllm:8000/v1",
            "LLM_UPSTREAM_BASE_URL": "https://llm.example/v1",
            "LLM_UPSTREAM_MODEL": " cloud-small ",
            "LLM_UPSTREAM_DROP_PARAMS": "user, ,seed",
            "LLM_PROXY_DAILY_REQUEST_LIMIT": "-5",
        }
    )

    assert settings.upstream_base_url == "https://llm.example/v1"
    assert settings.upstream_model == "cloud-small"
    assert settings.drop_params == frozenset({"user", "seed"})
    assert settings.daily_request_limit == 0


def test_keys_stay_out_of_the_repr():
    settings = config.load_settings(
        {"LLM_UPSTREAM_API_KEY": "sk-upstream", "LLM_PROXY_API_KEY": "client"}
    )

    assert "sk-upstream" not in repr(settings)
    assert "client" not in repr(settings).replace("client_", "")


@pytest.mark.parametrize("value", ["[1, 2]", "{broken"])
def test_extra_body_must_be_a_json_object(value):
    with pytest.raises(ValueError):
        config.load_settings({"LLM_UPSTREAM_EXTRA_BODY": value})


def test_daily_limit_starts_again_on_a_new_day():
    day = [datetime.date(2026, 10, 11)]
    limit = limits.DailyLimit(1, today=lambda: day[0])

    assert limit.try_acquire()
    assert not limit.try_acquire()
    day[0] = datetime.date(2026, 10, 12)
    assert limit.used == 0
    assert limit.try_acquire()


def test_daily_limit_zero_means_unlimited():
    limit = limits.DailyLimit(0)

    assert all(limit.try_acquire() for _ in range(1000))
