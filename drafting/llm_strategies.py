"""
Phase 3: Multi-LLM Backend Strategy Pattern
Supports: Ollama (primary) + Groq (fallback) with auto-failover

Usage:
    from llm_strategies import StrategySelector, OllamaStrategy, GroqStrategy
    
    selector = StrategySelector(
        primary=OllamaStrategy(url, model),
        fallback=GroqStrategy(api_key, model)
    )
    async for chunk in selector.get_streamer(prompt):
        yield chunk
"""

import httpx
import json
import asyncio
from abc import ABC, abstractmethod
from enum import Enum
from typing import AsyncGenerator, Dict, Optional


class LLMBackend(str, Enum):
    OLLAMA = "ollama"
    GROQ = "groq"


class LLMStrategy(ABC):
    """Abstract base class for LLM backends."""

    @abstractmethod
    async def stream(self, prompt: str) -> AsyncGenerator[str, None]:
        """Yield SSE-formatted tokens: data: {"token": "...", "done": bool}"""
        pass

    @abstractmethod
    async def health_check(self) -> Dict:
        """Return backend health status."""
        pass


class OllamaStrategy(LLMStrategy):
    """Local Ollama backend (primary - self-hosted, no API key needed)."""

    def __init__(self, url: str, model: str):
        self.url = url
        self.model = model
        self.timeout = 180.0
        self.name = "ollama"

    async def stream(self, prompt: str) -> AsyncGenerator[str, None]:
        """Stream from local Ollama server."""
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": True,
            "options": {
                "temperature": 0.25,
                "num_predict": 4096,
                "top_p": 0.9,
                "repeat_penalty": 1.1,
            },
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream("POST", self.url, json=payload) as response:
                    if response.status_code != 200:
                        yield f"data: {json.dumps({'error': f'Ollama HTTP {response.status_code}'})}\\n\\n"
                        return
                    async for line in response.aiter_lines():
                        if line:
                            try:
                                chunk = json.loads(line)
                                token = chunk.get("response", "")
                                done = chunk.get("done", False)
                                yield f"data: {json.dumps({'token': token, 'done': done})}\\n\\n"
                                if done:
                                    break
                            except json.JSONDecodeError:
                                continue
        except asyncio.TimeoutError:
            yield f"data: {json.dumps({'error': 'Ollama timeout — switching to Groq fallback'})}\\n\\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': f'Ollama error: {str(e)}'})}\\n\\n"

    async def health_check(self) -> Dict:
        """Check if Ollama is running and responding."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post(
                    self.url,
                    json={"model": self.model, "prompt": "health"},
                    timeout=5.0
                )
                return {
                    "status": "healthy" if r.status_code == 200 else "degraded",
                    "backend": "ollama",
                    "model": self.model,
                    "url": self.url,
                }
        except Exception as e:
            return {
                "status": "down",
                "backend": "ollama",
                "error": str(e),
                "url": self.url,
            }


class GroqStrategy(LLMStrategy):
    """Groq API backend (fast fallback, free tier at $0/month)."""

    def __init__(self, api_key: str, model: str = "mixtral-8x7b-32768"):
        self.api_key = api_key
        self.model = model
        self.base_url = "https://api.groq.com/openai/v1/chat/completions"
        self.timeout = 60.0
        self.name = "groq"

    async def stream(self, prompt: str) -> AsyncGenerator[str, None]:
        """Stream from Groq API (OpenAI-compatible format)."""
        if not self.api_key:
            yield f"data: {json.dumps({'error': 'GROQ_API_KEY not configured'})}\\n\\n"
            return

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 4096,
            "temperature": 0.25,
            "stream": True,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream("POST", self.base_url, headers=headers, json=payload) as response:
                    if response.status_code != 200:
                        body = await response.aread()
                        error_msg = body.decode()[:100]
                        yield f"data: {json.dumps({'error': f'Groq API {response.status_code}: {error_msg}'})}\\n\\n"
                        return
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            raw = line[6:]
                            if raw == "[DONE]":
                                yield f"data: {json.dumps({'token': '', 'done': True})}\\n\\n"
                                break
                            try:
                                evt = json.loads(raw)
                                choices = evt.get("choices", [{}])
                                delta = choices[0].get("delta", {})
                                token = delta.get("content", "")
                                finish_reason = choices[0].get("finish_reason")
                                finished = finish_reason is not None
                                yield f"data: {json.dumps({'token': token, 'done': finished})}\\n\\n"
                                if finished:
                                    break
                            except json.JSONDecodeError:
                                continue
        except asyncio.TimeoutError:
            yield f"data: {json.dumps({'error': 'Groq API timeout'})}\\n\\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': f'Groq error: {str(e)}'})}\\n\\n"

    async def health_check(self) -> Dict:
        """Check Groq API health and authentication."""
        if not self.api_key:
            return {"status": "not_configured", "backend": "groq"}
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                headers = {"Authorization": f"Bearer {self.api_key}"}
                r = await client.get(
                    "https://api.groq.com/openai/v1/models",
                    headers=headers,
                    timeout=5.0
                )
                return {
                    "status": "healthy" if r.status_code == 200 else "degraded",
                    "backend": "groq",
                    "model": self.model,
                }
        except Exception as e:
            return {
                "status": "down",
                "backend": "groq",
                "error": str(e),
            }


class StrategySelector:
    """Manages multiple LLM backends with intelligent fallback routing."""

    def __init__(self, primary: LLMStrategy, fallback: Optional[LLMStrategy] = None):
        self.primary = primary
        self.fallback = fallback
        self.last_used = primary
        self.consecutive_failures = 0

    async def get_streamer(self, prompt: str) -> AsyncGenerator[str, None]:
        """Stream from primary; gracefully fallback on timeout/error."""
        error_occurred = False
        try:
            async for chunk in self.primary.stream(prompt):
                yield chunk
                self.last_used = self.primary
                self.consecutive_failures = 0
        except Exception as e:
            error_occurred = True
            self.consecutive_failures += 1

        if error_occurred and self.fallback:
            yield f"data: {json.dumps({'warning': f'{self.primary.name} unavailable, using {self.fallback.name} (free tier)'})}\\n\\n"
            try:
                async for chunk in self.fallback.stream(prompt):
                    yield chunk
                    self.last_used = self.fallback
            except Exception as e:
                yield f"data: {json.dumps({'error': f'Fallback {self.fallback.name} also failed: {str(e)}'})}\\n\\n"
        elif error_occurred and not self.fallback:
            yield f"data: {json.dumps({'error': f'{self.primary.name} failed and no fallback configured'})}\\n\\n"

    async def health_status(self) -> Dict:
        """Get comprehensive health status of all backends."""
        primary_health = await self.primary.health_check()
        fallback_health = await self.fallback.health_check() if self.fallback else None
        return {
            "primary": primary_health,
            "fallback": fallback_health,
            "last_used": self.last_used.name,
            "consecutive_failures": self.consecutive_failures,
        }

    def switch_primary_on_failures(self, threshold: int = 3) -> bool:
        """Auto-switch to fallback if primary fails N times."""
        if self.consecutive_failures >= threshold and self.fallback:
            self.primary, self.fallback = self.fallback, self.primary
            self.consecutive_failures = 0
            return True
        return False
