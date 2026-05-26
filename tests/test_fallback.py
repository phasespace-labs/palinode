import pytest
import httpx
from unittest.mock import MagicMock
from palinode.core.config import config
from palinode.consolidation.runner import _call_llm_with_fallback, _build_model_chain

@pytest.fixture
def run_config(monkeypatch):
    monkeypatch.setattr(config.consolidation, "llm_model", "primary-model")
    monkeypatch.setattr(config.consolidation, "llm_url", "http://primary")
    monkeypatch.setattr(config.consolidation, "llm_fallbacks", [
        {"model": "fallback-1", "url": "http://fallback-1"},
        {"model": "fallback-2", "url": "http://fallback-2"}
    ])
    return config

def test_build_model_chain_order(run_config):
    """Chain is always: primary first, then fallbacks in config order"""
    chain = _build_model_chain()
    assert len(chain) == 3
    assert chain[0] == {"model": "primary-model", "url": "http://primary"}
    assert chain[1] == {"model": "fallback-1", "url": "http://fallback-1"}
    assert chain[2] == {"model": "fallback-2", "url": "http://fallback-2"}

def test_primary_succeeds_no_fallback(run_config, monkeypatch):
    """Primary model works → no fallback attempted"""
    mock_post = MagicMock()
    mock_post.return_value.json.return_value = {"choices": [{"message": {"content": "success"}}]}
    monkeypatch.setattr(httpx, "post", mock_post)

    result, model = _call_llm_with_fallback("sys", "user")
    
    assert result == "success"
    assert model == "primary-model"
    assert mock_post.call_count == 1
    assert mock_post.call_args[0][0] == "http://primary/v1/chat/completions"

def test_primary_timeout_uses_fallback(run_config, monkeypatch):
    """Primary times out → fallback model called and succeeds"""
    mock_post = MagicMock()
    mock_post.side_effect = [
        httpx.TimeoutException("timeout"),
        MagicMock(json=lambda: {"choices": [{"message": {"content": "fallback_success"}}]})
    ]
    monkeypatch.setattr(httpx, "post", mock_post)

    result, model = _call_llm_with_fallback("sys", "user")
    
    assert result == "fallback_success"
    assert model == "fallback-1"
    assert mock_post.call_count == 2
    assert mock_post.call_args_list[0][0][0] == "http://primary/v1/chat/completions"
    assert mock_post.call_args_list[1][0][0] == "http://fallback-1/v1/chat/completions"

def test_all_models_fail_raises(run_config, monkeypatch):
    """All models fail → RuntimeError raised"""
    mock_post = MagicMock()
    mock_post.side_effect = httpx.TimeoutException("timeout")
    monkeypatch.setattr(httpx, "post", mock_post)

    with pytest.raises(RuntimeError, match="All 3 models failed"):
        _call_llm_with_fallback("sys", "user")
    assert mock_post.call_count == 3

def test_fallback_logged(run_config, monkeypatch, caplog):
    """When fallback is used, it's logged with model name and URL"""
    import logging
    caplog.set_level(logging.INFO)
    mock_post = MagicMock()
    mock_post.side_effect = [
        httpx.TimeoutException("timeout"),
        MagicMock(json=lambda: {"choices": [{"message": {"content": "fb"}}]})
    ]
    monkeypatch.setattr(httpx, "post", mock_post)

    _call_llm_with_fallback("sys", "user")
    
    log_text = caplog.text
    assert "Model primary-model @ http://primary failed" in log_text
    assert "Fallback model succeeded: fallback-1 @ http://fallback-1" in log_text

def test_empty_fallbacks_primary_only(monkeypatch):
    """No fallbacks configured → only primary attempted"""
    monkeypatch.setattr(config.consolidation, "llm_model", "primary")
    monkeypatch.setattr(config.consolidation, "llm_url", "http://primary")
    monkeypatch.setattr(config.consolidation, "llm_fallbacks", [])
    
    mock_post = MagicMock()
    mock_post.side_effect = httpx.TimeoutException("timeout")
    monkeypatch.setattr(httpx, "post", mock_post)

    with pytest.raises(RuntimeError, match="All 1 models failed"):
        _call_llm_with_fallback("sys", "user")
    assert mock_post.call_count == 1

def test_model_used_returned(run_config, monkeypatch):
    """Return value includes which model handled the request"""
    # tested in above cases as well
    mock_post = MagicMock()
    mock_post.side_effect = [
        httpx.TimeoutException("timeout"),
        httpx.TimeoutException("timeout"),
        MagicMock(json=lambda: {"choices": [{"message": {"content": "fb2"}}]})
    ]
    monkeypatch.setattr(httpx, "post", mock_post)

    _, model = _call_llm_with_fallback("sys", "user")
    assert model == "fallback-2"

def test_fallback_different_url(run_config, monkeypatch):
    """Fallback can point to a different endpoint than primary"""
    mock_post = MagicMock()
    mock_post.side_effect = [
        httpx.TimeoutException("timeout"),
        MagicMock(json=lambda: {"choices": [{"message": {"content": "success"}}]})
    ]
    monkeypatch.setattr(httpx, "post", mock_post)

    _, model = _call_llm_with_fallback("sys", "user")
    assert mock_post.call_args_list[0][0][0] == "http://primary/v1/chat/completions"
    assert mock_post.call_args_list[1][0][0] == "http://fallback-1/v1/chat/completions"
