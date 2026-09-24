import asyncio
import traceback

from aioice import turn


async def main() -> None:
    for label, kwargs in [
        (
            "freeturn udp",
            dict(
                server_addr=("freeturn.net", 3478),
                username="free",
                password="free",
                ssl=False,
                transport="udp",
            ),
        ),
        (
            "openrelay udp",
            dict(
                server_addr=("openrelay.metered.ca", 80),
                username="openrelayproject",
                password="openrelayproject",
                ssl=False,
                transport="udp",
            ),
        ),
    ]:
        print("===", label)
        try:
            transport, protocol = await asyncio.wait_for(
                turn.create_turn_endpoint(
                    protocol_factory=lambda: None,
                    **kwargs,
                ),
                timeout=8,
            )
            print("OK local", protocol.local_candidate)
        except Exception as e:
            print("ERR", type(e).__name__, e)
            traceback.print_exc()


asyncio.run(main())
