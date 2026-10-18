"""llama-server 的极简客户端（只用标准库）。

llama.cpp 的 server 提供 OpenAI 兼容接口，所以这里只实现我们真正用到的那一个：
`POST /v1/chat/completions`，非流式。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class ChatError(RuntimeError):
    """请求失败。"""


class ChatClient:
    """对本地 llama-server 的最小封装。"""

    def __init__(self, port: int = 8080, timeout: float = 300.0):
        self.base = f"http://127.0.0.1:{port}/v1"
        self.timeout = timeout

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> str:
        """发一次对话请求，返回助手回复的纯文本。"""
        payload = json.dumps(
            {
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            f"{self.base}/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise ChatError(f"HTTP {exc.code}：{detail}") from exc
        except urllib.error.URLError as exc:
            raise ChatError(f"连接失败（llama-server 没起来？）：{exc.reason}") from exc

        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise ChatError(f"响应格式异常：{json.dumps(data)[:400]}") from exc

    def ask(self, prompt: str, *, system: str = "", **kwargs) -> str:
        """单轮提问的快捷方式。"""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self.chat(messages, **kwargs)
