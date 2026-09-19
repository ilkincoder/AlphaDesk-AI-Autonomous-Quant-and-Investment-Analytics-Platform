"""The DeepSeek client: request shape, error classification, and what gets retried.

This is the only module that knows anything provider-specific, so this is where the provider
rules are asserted. Two of them are recorded here because they were verified against the live
endpoint rather than assumed: thinking mode is switched off explicitly (it is on by default),
and no sampling parameters are sent (the provider documents them as having no effect).

The retry policy lives here rather than in the run, because only this module can tell a rate
limit from a wrong key. A wrong key stays wrong, and retrying it spends the run's budget to
learn the same thing more slowly.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from types import SimpleNamespace

import httpx
import openai

from app.agent.budget import BudgetExhausted, RunBudget
from app.agent.llm import (
    DeepSeekClient,
    ProviderAuthError,
    ProviderResponseError,
    ProviderUnavailableError,
    ToolCall,
    assistant_message,
    build_client,
)

URL = "https://api.deepseek.com/chat/completions"


def a_request() -> httpx.Request:
    return httpx.Request("POST", URL)


def an_error(kind: str, status: int) -> Exception:
    """One of the SDK's error types, built the way the SDK builds them."""
    request = a_request()
    response = httpx.Response(status, request=request)
    if kind == "auth":
        return openai.AuthenticationError("bad key", response=response, body=None)
    if kind == "permission":
        return openai.PermissionDeniedError("not allowed", response=response, body=None)
    if kind == "rate":
        return openai.RateLimitError("slow down", response=response, body=None)
    if kind == "server":
        return openai.InternalServerError("boom", response=response, body=None)
    if kind == "bad_request":
        return openai.BadRequestError("bad params", response=response, body=None)
    if kind == "timeout":
        return openai.APITimeoutError(request=request)
    if kind == "connection":
        return openai.APIConnectionError(request=request)
    raise AssertionError(kind)


def a_completion(*, content="hello", tool_calls=None, prompt=10, completion=5):
    """A completion object with the shape the SDK returns."""
    message = SimpleNamespace(
        content=content,
        tool_calls=[
            SimpleNamespace(
                id=item["id"],
                function=SimpleNamespace(
                    name=item["name"], arguments=item["arguments"]
                ),
            )
            for item in (tool_calls or [])
        ],
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(
            prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion
        ),
    )


class FakeOpenAI:
    """The SDK's client, reduced to the one call this code makes."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.payloads = []

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **payload):
        self.payloads.append(payload)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def a_client(outcomes, budget=None, model="deepseek-flash"):
    return DeepSeekClient(
        api_key="test-key",
        model_name=model,
        budget=budget,
        sleep=lambda _: None,
        _client=FakeOpenAI(outcomes),
    )


class RequestShapeTests(unittest.TestCase):
    def complete(self, client):
        return client.complete(
            messages=[{"role": "user", "content": "hi"}], max_tokens=321
        )

    def test_thinking_mode_is_turned_off_explicitly(self):
        """It is enabled by the provider by default, at `high` effort."""
        client = a_client([a_completion()])

        self.complete(client)

        self.assertEqual(
            client._client.payloads[0]["extra_body"], {"thinking": {"type": "disabled"}}
        )

    def test_no_sampling_parameters_are_sent(self):
        """The provider documents these as having no effect in non-thinking mode."""
        client = a_client([a_completion()])

        self.complete(client)

        payload = client._client.payloads[0]
        for parameter in ("temperature", "top_p", "frequency_penalty", "presence_penalty"):
            self.assertNotIn(parameter, payload)

    def test_the_output_token_ceiling_is_sent_explicitly(self):
        """Otherwise the provider's own default applies, which nobody here chose."""
        client = a_client([a_completion()])

        self.complete(client)

        self.assertEqual(client._client.payloads[0]["max_tokens"], 321)

    def test_tools_and_the_response_format_are_sent_only_when_asked_for(self):
        client = a_client([a_completion()])

        self.complete(client)

        payload = client._client.payloads[0]
        self.assertNotIn("tools", payload)
        self.assertNotIn("response_format", payload)

    def test_the_model_and_messages_are_sent(self):
        client = a_client([a_completion()], model="deepseek-flash")

        self.complete(client)

        payload = client._client.payloads[0]
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "hi"}])


class ResponseReadingTests(unittest.TestCase):
    def test_content_and_usage_are_read_out(self):
        client = a_client([a_completion(content="hi there", prompt=100, completion=20)])

        response = client.complete(messages=[], max_tokens=10)

        self.assertEqual(response.content, "hi there")
        self.assertEqual(response.prompt_tokens, 100)
        self.assertEqual(response.completion_tokens, 20)
        self.assertEqual(response.total_tokens, 120)

    def test_tool_calls_are_read_with_their_ids_and_raw_arguments(self):
        client = a_client(
            [
                a_completion(
                    content=None,
                    tool_calls=[
                        {"id": "call-1", "name": "portfolio_context",
                         "arguments": '{"symbol": "NVDA"}'}
                    ],
                )
            ]
        )

        response = client.complete(messages=[], max_tokens=10)

        self.assertEqual(len(response.tool_calls), 1)
        self.assertEqual(response.tool_calls[0].id, "call-1")
        self.assertEqual(response.tool_calls[0].name, "portfolio_context")
        # Kept unparsed: a malformed argument string is the dispatcher's to report.
        self.assertEqual(response.tool_calls[0].arguments, '{"symbol": "NVDA"}')
        self.assertTrue(response.wants_tools)

    def test_the_appended_message_is_the_provider_wire_shape(self):
        client = a_client(
            [
                a_completion(
                    content="checking",
                    tool_calls=[
                        {"id": "call-1", "name": "portfolio_context", "arguments": "{}"}
                    ],
                )
            ]
        )

        response = client.complete(messages=[], max_tokens=10)

        self.assertEqual(response.message["role"], "assistant")
        self.assertEqual(response.message["content"], "checking")
        self.assertEqual(
            response.message["tool_calls"],
            [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "portfolio_context", "arguments": "{}"},
                }
            ],
        )

    def test_a_reply_with_no_choices_is_a_response_error(self):
        client = a_client([SimpleNamespace(choices=[], usage=None)])

        with self.assertRaises(ProviderResponseError):
            client.complete(messages=[], max_tokens=10)


class ErrorClassificationTests(unittest.TestCase):
    """Which failures are worth another attempt, and which are not."""

    def test_authentication_is_never_retried(self):
        client = a_client([an_error("auth", 401)])

        with self.assertRaises(ProviderAuthError) as caught:
            client.complete(messages=[], max_tokens=10)

        self.assertIn("DEEPSEEK_API_KEY", str(caught.exception))
        # One attempt: a wrong key stays wrong.
        self.assertEqual(len(client._client.payloads), 1)

    def test_a_permission_failure_is_a_credentials_problem_too(self):
        client = a_client([an_error("permission", 403)])

        with self.assertRaises(ProviderAuthError):
            client.complete(messages=[], max_tokens=10)
        self.assertEqual(len(client._client.payloads), 1)

    def test_a_rejected_request_is_not_retried(self):
        """It reached the provider and was refused; the same request gets the same answer."""
        client = a_client([an_error("bad_request", 400)])

        with self.assertRaises(ProviderResponseError):
            client.complete(messages=[], max_tokens=10)
        self.assertEqual(len(client._client.payloads), 1)

    def test_an_unexpected_sdk_error_is_a_response_error(self):
        client = a_client([openai.OpenAIError("something odd")])

        with self.assertRaises(ProviderResponseError):
            client.complete(messages=[], max_tokens=10)

    def test_a_rate_limit_is_retried_and_can_succeed(self):
        budget = RunBudget()
        client = a_client([an_error("rate", 429), a_completion()], budget=budget)

        response = client.complete(messages=[], max_tokens=10)

        self.assertEqual(response.content, "hello")
        self.assertEqual(len(client._client.payloads), 2)
        self.assertEqual(budget.transient_retries, 1)

    def test_a_server_error_and_a_timeout_and_a_connection_error_are_retried(self):
        for kind, status in (("server", 500), ("timeout", 0), ("connection", 0)):
            with self.subTest(kind=kind):
                budget = RunBudget()
                client = a_client([an_error(kind, status), a_completion()], budget=budget)

                client.complete(messages=[], max_tokens=10)

                self.assertEqual(budget.transient_retries, 1)

    def test_the_retry_allowance_is_a_ceiling_not_a_loop(self):
        budget = RunBudget(max_transient_retries=1)
        client = a_client(
            [an_error("timeout", 0), an_error("timeout", 0), a_completion()],
            budget=budget,
        )

        with self.assertRaises(ProviderUnavailableError):
            client.complete(messages=[], max_tokens=10)

        # One attempt plus one retry, and the third outcome is never reached.
        self.assertEqual(len(client._client.payloads), 2)
        self.assertEqual(budget.transient_retries, 1)

    def test_a_provider_that_stays_down_is_a_provider_failure_not_a_budget_one(self):
        """The distinction names the cause, and the cause is what a reader acts on."""
        budget = RunBudget(max_transient_retries=2)
        client = a_client(
            [an_error("server", 500) for _ in range(3)], budget=budget
        )

        with self.assertRaises(ProviderUnavailableError):
            client.complete(messages=[], max_tokens=10)

        self.assertEqual(len(client._client.payloads), 3)
        self.assertEqual(budget.transient_retries, 2)
        # Nothing recorded a budget stop: the run's budget is not what went wrong.
        self.assertIsNone(budget.stopped_by)

    def test_with_no_budget_the_client_retries_nothing(self):
        """A caller that has not thought about budgets gets the failure, not a hidden retry."""
        client = a_client([an_error("timeout", 0), a_completion()], budget=None)

        with self.assertRaises(ProviderUnavailableError):
            client.complete(messages=[], max_tokens=10)
        self.assertEqual(len(client._client.payloads), 1)

    def test_the_deadline_stops_a_retry(self):
        budget = RunBudget(deadline_seconds=0.0)
        client = a_client([an_error("timeout", 0), a_completion()], budget=budget)

        with self.assertRaises(BudgetExhausted) as caught:
            client.complete(messages=[], max_tokens=10)

        self.assertEqual(caught.exception.limit, "deadline_seconds")


class ClientConstructionTests(unittest.TestCase):
    def test_a_missing_key_is_a_clear_configuration_error(self):
        settings = SimpleNamespace(
            deepseek_api_key=None,
            deepseek_model="deepseek-flash",
            deepseek_base_url="https://api.deepseek.com",
            deepseek_timeout_seconds=60.0,
        )

        with self.assertRaises(ProviderAuthError) as caught:
            build_client(settings)

        self.assertIn("DEEPSEEK_API_KEY", str(caught.exception))

    def test_the_configured_values_reach_the_client(self):
        settings = SimpleNamespace(
            deepseek_api_key="a-key",
            deepseek_model="a-model",
            deepseek_base_url="https://example.invalid",
            deepseek_timeout_seconds=12.0,
        )

        client = build_client(settings, RunBudget())

        self.assertEqual(client.model_name, "a-model")
        self.assertEqual(client.base_url, "https://example.invalid")
        self.assertEqual(client.timeout_seconds, 12.0)

    def test_the_sdk_s_own_retries_are_switched_off(self):
        """Retrying is the run's decision; a silent SDK retry would be uncounted spending."""
        client = a_client([a_completion()])
        from app.agent.llm import DeepSeekClient

        built = DeepSeekClient(api_key="x", sleep=lambda _: None)
        self.assertEqual(built._client.max_retries, 0)
        self.assertEqual(client._client.payloads, [])


class MessageShapeTests(unittest.TestCase):
    def test_an_assistant_turn_without_tool_calls_has_no_tool_calls_key(self):
        message = assistant_message(content="hello", tool_calls=())

        self.assertEqual(message, {"role": "assistant", "content": "hello"})

    def test_a_tool_call_turn_carries_every_id_verbatim(self):
        message = assistant_message(
            content=None,
            tool_calls=(ToolCall(id="a", name="t", arguments="{}"),),
        )

        self.assertEqual(message["tool_calls"][0]["id"], "a")
        self.assertEqual(message["tool_calls"][0]["type"], "function")


if __name__ == "__main__":
    unittest.main()
