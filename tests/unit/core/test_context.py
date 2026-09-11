"""Per-request context, and the task-locality the whole isolation story rests on."""

from __future__ import annotations

import asyncio

from persona_api.core.context import (
    bind_account_id,
    bind_request_id,
    get_account_id,
    get_request_id,
    new_request_id,
    set_account_id,
)


class TestRequestIds:
    def test_a_fresh_id_is_unique(self) -> None:
        assert new_request_id() != new_request_id()

    def test_nothing_is_bound_outside_a_request(self) -> None:
        assert get_request_id() is None

    def test_binding_restores_the_previous_value_afterwards(self) -> None:
        with bind_request_id("outer"), bind_request_id("inner"):
            assert get_request_id() == "inner"

        assert get_request_id() is None


class TestAccountIds:
    def test_nothing_is_bound_for_an_anonymous_request(self) -> None:
        assert get_account_id() is None

    def test_binding_restores_the_previous_value_afterwards(self) -> None:
        with bind_account_id("acct_one"):
            assert get_account_id() == "acct_one"

        assert get_account_id() is None

    async def test_set_without_a_matching_unbind_still_ends_with_the_task(self) -> None:
        # set_account_id has no unbind because its caller is a FastAPI dependency: the
        # binding has to outlive the dependency and cover the handler, and a context
        # manager cannot span the two. It is safe because context variables are
        # task-local -- which is a claim worth testing rather than asserting.
        async def request() -> str | None:
            set_account_id("acct_one")
            return get_account_id()

        assert await asyncio.create_task(request()) == "acct_one"
        assert get_account_id() is None


class TestTaskLocality:
    async def test_two_concurrent_requests_never_see_each_others_account(self) -> None:
        # This is the mechanism the entire isolation story rests on. If a context
        # variable leaked between tasks, every store call in the service would be
        # scoped to whichever request happened to bind last -- so it is tested by
        # running them concurrently rather than by reading the contextvars docs.
        started = asyncio.Event()

        async def first() -> str | None:
            set_account_id("acct_one")
            started.set()
            await asyncio.sleep(0)
            return get_account_id()

        async def second() -> str | None:
            await started.wait()
            set_account_id("acct_two")
            return get_account_id()

        assert list(await asyncio.gather(first(), second())) == ["acct_one", "acct_two"]

    async def test_a_binding_does_not_leak_into_the_next_request_on_the_same_worker(
        self,
    ) -> None:
        async def request(account_id: str) -> str | None:
            set_account_id(account_id)
            return get_account_id()

        assert await asyncio.create_task(request("acct_one")) == "acct_one"
        # A fresh task, as the next request served by the same worker would be.
        assert await asyncio.create_task(request("acct_two")) == "acct_two"
        assert get_account_id() is None
