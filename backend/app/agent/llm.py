"""The only place that talks to DeepSeek.

Everything provider-specific lives here: the base URL, the model, the `thinking` parameter,
the request timeout, the retry policy and the error taxonomy. The two graphs above it see a
`ModelClient` and a `ModelResponse`, which is what lets them be tested against a scripted
double with no network and no key.

**Non-thinking mode is switched on explicitly, not left to the default.** DeepSeek's thinking
mode is enabled by default at `high` effort, and it is turned off here with
`extra_body={"thinking": {"type": "disabled"}}` for two reasons. It bounds latency and cost on
a flow whose whole job is to pick between four tools and summarise what they returned. And it
removes a failure mode specific to thinking mode with tools: every earlier turn's
`reasoning_content` has to be echoed back on every subsequent request or the API returns 400.
Verified against the live endpoint -- `reasoning_content` is absent from a disabled-mode
response, and a tool call round trip completes.

**Sampling parameters are deliberately not sent.** The provider documents `temperature`,
`frequency_penalty` and `presence_penalty` as having no effect, and `top_p` as fixed at 1.0 in
non-thinking mode. Sending them would suggest a control this run does not have.

**Errors are sorted into two kinds, and the difference matters.** A rate limit, a timeout or a
5xx is transient and worth retrying a bounded number of times. An authentication or permission
failure is not: retrying it burns the run's budget to reach the same answer, and it is the
run's job to say so clearly instead. Everything else is reported as a response error, which
means the request reached the provider and the provider did not like it -- a bug on this side,
not a network problem.
"""

import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import openai

from app.agent.budget import RunBudget

logger = logging.getLogger(__name__)

# The provider's own name for the switch that turns thinking off. Named rather than inlined
# because it is a string the provider defines and this code cannot check at import time.
_THINKING_DISABLED = {"thinking": {"type": "disabled"}}

# Seconds before the first retry. Short, because the failures worth retrying are usually
# momentary, and the run's deadline is the real ceiling.
_RETRY_BASE_SECONDS = 0.5


class ModelError(Exception):
    """Base class for every failure this module reports."""


class ProviderAuthError(ModelError):
    """The provider rejected the credentials or the account.

    Never retried. A wrong key stays wrong, and a run that retries it spends its budget to
    learn the same thing more slowly.
    """


class ProviderUnavailableError(ModelError):
    """The provider could not be reached, or asked us to come back later."""


class ProviderResponseError(ModelError):
    """The request reached the provider and was rejected, or the reply could not be read."""


@dataclass(frozen=True)
class ToolCall:
    """One function call the model asked for.

    `arguments` is the raw JSON *string* the provider sent, kept unparsed. Parsing it is the
    dispatcher's job, because a malformed argument string is a condition the model can be told
    about and correct -- turning it into an exception here would end the run instead.
    """

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ModelResponse:
    """One completion, plus the message that has to be appended to the conversation.

    `message` is a plain dict in the provider's own wire shape rather than a provider object.
    That is what makes the scripted test double able to produce byte-identical message
    history to the real client: the graph appends `response.message` and never has to know
    which of the two produced it.
    """

    content: str | None
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str | None
    message: dict[str, Any]
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


def assistant_message(
    *, content: str | None, tool_calls: Sequence[ToolCall]
) -> dict[str, Any]:
    """The assistant turn, in the wire shape the provider expects to see again.

    A tool-calling assistant turn is appended **verbatim**, ids included: the ids are how the
    following `role: "tool"` messages are matched to the calls they answer, and a rewritten
    or dropped id produces a request the provider rejects.
    """
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in tool_calls
        ]
    return message


class ModelClient(Protocol):
    """What the graphs need from a model. Narrow on purpose."""

    model_name: str

    def complete(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        response_format: Mapping[str, Any] | None = None,
        max_tokens: int,
    ) -> ModelResponse: ...


@dataclass
class DeepSeekClient:
    """DeepSeek over its OpenAI-compatible endpoint.

    Constructed inside a run, never at import: the API must start and serve its existing
    endpoints whether or not a key is configured.
    """

    api_key: str
    model_name: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    timeout_seconds: float = 60.0
    # The retry allowance is the run's, not the client's: it is counted by the same budget
    # that counts model requests and tool calls, so the run result reports one number and
    # there is no second ceiling to keep in step. None means "retry nothing", which is what
    # a caller that has not thought about budgets should get.
    budget: RunBudget | None = None
    # Injected for tests: keeps them from waiting on a real backoff.
    sleep: Any = time.sleep
    _client: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self._client is None:
            self._client = openai.OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                # The SDK's own retries are switched off. Retrying is a decision about the
                # run's budget, and it belongs somewhere that can count.
                max_retries=0,
            )

    def complete(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        response_format: Mapping[str, Any] | None = None,
        max_tokens: int,
    ) -> ModelResponse:
        """One completion, retried only for failures that are worth retrying.

        `ProviderAuthError` and `ProviderResponseError` are not caught here at all: a wrong
        key and a malformed request both produce the same outcome on every attempt, so
        retrying them only spends the run's budget to reach the same answer.
        """
        attempt = 0
        while True:
            try:
                return self._request(
                    messages=messages,
                    tools=tools,
                    response_format=response_format,
                    max_tokens=max_tokens,
                )
            except ProviderUnavailableError as exc:
                if self.budget is None or not self.budget.retry_allowed():
                    # Out of retries, or nothing is counting them. The provider stayed down,
                    # and that is what the caller is told -- not budget exhaustion, which
                    # would name a cause that is not the one.
                    raise
                # Raises `BudgetExhausted` if the run's deadline has passed.
                self.budget.spend_retry()
                attempt += 1
                delay = _RETRY_BASE_SECONDS * attempt
                logger.warning(
                    "provider request failed (%s), retrying in %.1fs (attempt %d)",
                    type(exc).__name__,
                    delay,
                    attempt,
                )
                self.sleep(delay)

    def _request(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None,
        response_format: Mapping[str, Any] | None,
        max_tokens: int,
    ) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": list(messages),
            "max_tokens": max_tokens,
            # The one provider-specific knob. `extra_body` is how the OpenAI SDK sends a
            # field the OpenAI API does not have.
            "extra_body": dict(_THINKING_DISABLED),
        }
        if tools:
            payload["tools"] = list(tools)
        if response_format is not None:
            payload["response_format"] = dict(response_format)

        try:
            completion = self._client.chat.completions.create(**payload)
        except openai.AuthenticationError as exc:
            raise ProviderAuthError(
                "DeepSeek rejected the configured credentials. Check DEEPSEEK_API_KEY."
            ) from exc
        except openai.PermissionDeniedError as exc:
            raise ProviderAuthError(
                "the DeepSeek account is not permitted to use this model."
            ) from exc
        except (
            openai.RateLimitError,
            openai.APITimeoutError,
            openai.APIConnectionError,
            openai.InternalServerError,
        ) as exc:
            raise ProviderUnavailableError(
                f"the provider was unavailable: {type(exc).__name__}"
            ) from exc
        except openai.APIStatusError as exc:
            # Reached the provider and was rejected. Retrying sends the same bad request.
            raise ProviderResponseError(
                f"the provider rejected the request: HTTP {exc.status_code}"
            ) from exc
        except openai.OpenAIError as exc:
            raise ProviderResponseError(
                f"the request could not be completed: {type(exc).__name__}"
            ) from exc

        return _to_response(completion)


def _to_response(completion: Any) -> ModelResponse:
    """Read one completion into this module's own shape."""
    if not getattr(completion, "choices", None):
        raise ProviderResponseError("the provider returned no choices")

    choice = completion.choices[0]
    message = choice.message
    raw_calls = getattr(message, "tool_calls", None) or []

    calls = tuple(
        ToolCall(
            id=str(call.id),
            name=str(call.function.name),
            # Kept as the provider sent it. A tool call whose arguments are not valid JSON is
            # something to tell the model about, not something to raise on here.
            arguments=call.function.arguments or "{}",
        )
        for call in raw_calls
    )

    usage = getattr(completion, "usage", None)
    return ModelResponse(
        content=message.content,
        tool_calls=calls,
        finish_reason=getattr(choice, "finish_reason", None),
        message=assistant_message(content=message.content, tool_calls=calls),
        prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
    )


def build_client(settings: Any, budget: RunBudget | None = None) -> DeepSeekClient:
    """The configured client, or a `ProviderAuthError` naming what is missing.

    Called from inside a run rather than at import, so importing this module -- or starting
    the API -- never requires a key.
    """
    api_key = getattr(settings, "deepseek_api_key", None)
    if not api_key:
        raise ProviderAuthError(
            "no DeepSeek API key is configured. Set DEEPSEEK_API_KEY in the environment "
            "(see .env.example); the key is only needed by `python -m app.run_analysis`."
        )
    return DeepSeekClient(
        api_key=api_key,
        model_name=settings.deepseek_model,
        base_url=settings.deepseek_base_url,
        timeout_seconds=settings.deepseek_timeout_seconds,
        budget=budget,
    )


__all__ = [
    "DeepSeekClient",
    "ModelClient",
    "ModelError",
    "ModelResponse",
    "ProviderAuthError",
    "ProviderResponseError",
    "ProviderUnavailableError",
    "ToolCall",
    "assistant_message",
    "build_client",
]
