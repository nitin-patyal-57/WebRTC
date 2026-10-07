import asyncio

from aioice.stun import Class, Message, Method, Transaction

from aioice_compat import install_aioice_retry_guard


def test_retry_callback_ignores_completed_transaction() -> None:
    install_aioice_retry_guard()

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        transaction = Transaction(
            request=Message(Method.BINDING, Class.REQUEST),
            addr=("127.0.0.1", 3478),
            protocol=type(
                "Protocol",
                (),
                {"send_stun": lambda self, message, addr: None},
            )(),
        )

        # A successful response completes the future before the retry timer fires.
        transaction.response_received(
            Message(Method.BINDING, Class.RESPONSE),
            ("127.0.0.1", 3478),
        )
        transaction._Transaction__tries = transaction._Transaction__tries_max

        # This is the exact callback state that previously raised InvalidStateError.
        transaction._Transaction__retry()

        assert transaction._Transaction__future.done()
    finally:
        loop.close()
        asyncio.set_event_loop(None)
