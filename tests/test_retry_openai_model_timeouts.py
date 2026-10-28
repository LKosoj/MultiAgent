import pytest

from retry_openai_model import RetryOpenAIServerModel


@pytest.mark.parametrize(('configured', 'expected'), [(None, 1200.0), (1200.0, 1200.0), (30.0, 30.0)])
def test_default_read_timeout_is_twenty_minutes_and_explicit_timeout_wins(configured, expected):
    model = object.__new__(RetryOpenAIServerModel)
    model.timeout_seconds = configured
    model.retry_delay_base = 1.0
    with model._create_custom_http_client(max_retries=0) as client:
        assert client.timeout.read == expected
