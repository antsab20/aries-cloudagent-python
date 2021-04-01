import asyncio
import json
import logging
import os
import sys
import random

from datetime import date
from uuid import uuid4

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa

from runners.agent_container import (
    create_agent_with_args,
    arg_parser,
)
from runners.support.agent import DemoAgent, default_genesis_txns  # noqa:E402
from runners.support.utils import (  # noqa:E402
    log_msg,
    log_status,
    log_timer,
    prompt,
    prompt_loop,
    require_indy,
)

LOGGER = logging.getLogger(__name__)


CRED_PREVIEW_TYPE = "https://didcomm.org/issue-credential/2.0/credential-preview"
SELF_ATTESTED = os.getenv("SELF_ATTESTED")
TAILS_FILE_COUNT = int(os.getenv("TAILS_FILE_COUNT", 100))

class AcmeAgent(DemoAgent):
    def __init__(self, http_port: int, admin_port: int, **kwargs):
        super().__init__(
            "Acme Agent",
            http_port,
            admin_port,
            prefix="Acme",
            extra_args=["--auto-accept-invites", "--auto-accept-requests"],
            **kwargs,
        )
        self.connection_id = None
        self._connection_ready = asyncio.Future()
        self.cred_state = {}
        # TODO define a dict to hold credential attributes based on
        # the cred_def_id
        self.cred_attrs = {}

    async def detect_connection(self):
        await self._connection_ready

    @property
    def connection_ready(self):
        return self._connection_ready.done() and self._connection_ready.result()

    async def handle_oob_invitation(self, message):
        pass

    async def handle_connections(self, message):
        conn_id = message["connection_id"]
        if message["rfc23_state"] == "invitation-sent":
            self.connection_id = conn_id
        if conn_id == self.connection_id:
            if (
                    message["rfc23_state"] in ["completed", "response-sent"]
                    and not self._connection_ready.done()
            ):
                self.log("Connected")
                self._connection_ready.set_result(True)

    async def handle_issue_credential_v2_0(self, message):
        state = message["state"]
        cred_ex_id = message["cred_ex_id"]
        prev_state = self.cred_state.get(cred_ex_id)
        if prev_state == state:
            return  # ignore
        self.cred_state[cred_ex_id] = state

        self.log(f"Credential: state = {state}, cred_ex_id = {cred_ex_id}")

        if state == "request-received":
            # TODO issue credentials based on offer preview in cred ex record
            await self.admin_POST(
                f"/issue-credential-2.0/records/{cred_ex_id}/issue",
                {"comment": f"Issuing credential, exchange {cred_ex_id}"},
            )

    async def handle_issue_credential_v2_0_indy(self, message):
        pass  # employee id schema does not support revocation

    async def handle_present_proof_v2_0(self, message):
        state = message["state"]
        pres_ex_id = message["pres_ex_id"]
        self.log(f"Presentation: state = {state}, pres_ex_id = {pres_ex_id}")

        if state == "presentation-received":
            state = message["state"]
            pres_ex_id = message["pres_ex_id"]
            self.log(f"Presentation: state = {state}, pres_ex_id = {pres_ex_id}")

            if state == "presentation-received":
                log_status("#27 Process the proof provided by X")
                log_status("#28 Check if proof is valid")
                proof = await self.admin_POST(
                    f"/present-proof-2.0/records/{pres_ex_id}/verify-presentation"
                )
                self.log("Proof =", proof["verified"])
                print(proof)

    async def handle_basicmessages(self, message):
        self.log("Received message:", message["content"])


async def main(start_port: int, show_timing: bool = False):

    genesis = await default_genesis_txns()
    if not genesis:
        print("Error retrieving ledger genesis transactions")
        sys.exit(1)

    agent = None

    try:
        log_status("#1 Provision an agent and wallet, get back configuration details")
        agent = AcmeAgent(
            start_port, start_port + 1, genesis_data=genesis, timing=show_timing
        )
        await agent.listen_webhooks(start_port + 2)
        await agent.register_did()

        with log_timer("Startup duration:"):
            await agent.start_process()
        log_msg("Admin URL is at:", agent.admin_url)
        log_msg("Endpoint URL is at:", agent.endpoint)

        # Create a schema
        log_status("#3 Create a new schema on the ledger")
        with log_timer("Publish schema and cred def duration:"):
            version = format(
                "%d.%d.%d"
                % (
                    random.randint(1, 101),
                    random.randint(1, 101),
                    random.randint(1, 101),
                )
            )
            (schema_id, cred_def_id) = await agent.register_schema_and_creddef(
                "employee id schema",
                version,
                ["employee_id", "name", "date", "position"],
                support_revocation=False,
                revocation_registry_size=TAILS_FILE_COUNT,
            )

        with log_timer("Generate invitation duration:"):
            # Generate an invitation
            log_status(
                "#5 Create a connection to alice and print out the invite details"
            )
            invi_msg = await agent.admin_POST(
                "/out-of-band/create-invitation",
                {"handshake_protocols": ["rfc23"]},
            )

        log_msg(
            json.dumps(invi_msg["invitation"]), label="Invitation Data:", color=None
        )
        log_msg("*****************")

        log_msg("Waiting for connection...")
        await agent.detect_connection()

        async for option in prompt_loop(
                "(1) Issue Credential, (2) Send Proof Request, "
                + "(3) Send Message (X) Exit? [1/2/3/X] "
        ):
            option = option.strip()
            if option in "xX":
                break

            elif option == "1":
                log_status("#13 Issue credential offer to X")
                # TODO credential offers
                agent.cred_attrs[cred_def_id] = {
                    "employee_id": "ACME0009",
                    "name": "Alice Smith",
                    "date": date.isoformat(date.today()),
                    "position": "CEO"
                }

                cred_preview = {
                    "@type": CRED_PREVIEW_TYPE,
                    "attributes": [
                        {"name": n, "value": v}
                        for (n, v) in agent.cred_attrs[
                            cred_def_id
                        ].items()
                    ],
                }
                offer_request = {
                    "connection_id": agent.connection_id,
                    "comment": f"Offer on cred def id {cred_def_id}",
                    "auto_remove": False,
                    "credential_preview": cred_preview,
                    "filter": {"indy": {"cred_def_id": cred_def_id}}
                }
                await agent.admin_POST(
                    "/issue-credential-2.0/send-offer", offer_request
                )
            elif option == "2":
                log_status("#20 Request proof of degree from alice")
                # TODO presentation requests
                faber_schema_name = "degree schema"
                req_attrs = [
                    {
                        "name": "name",
                        "restrictions": [{"schema_name": faber_schema_name}],
                    },
                    {
                        "name": "date",
                        "restrictions": [{"schema_name": faber_schema_name}],
                    },
                    {
                        "name": "degree",
                        "restrictions": [{"schema_name": faber_schema_name}],
                    }
                ]

                req_preds = [
                    # test zero-knowledge proofs
                    {
                        "name": "age",
                        "p_type": ">=",
                        "p_value": 18,
                        "restrictions": [{"schema_name": faber_schema_name}],
                    }
                ]

                indy_proof_request = {
                    "name": "Proof of Education",
                    "version": "1.0",
                    "requested_attributes": {
                        f"0_{req_attr['name']}_uuid": req_attr for req_attr in req_attrs
                    },
                    "requested_predicates": {
                        f"0_{req_pred['name']}_GE_uuid": req_pred
                        for req_pred in req_preds
                    },
                }

                proof_request_web_request = {
                    "connection_id": agent.connection_id,
                    "presentation_request": {"indy": indy_proof_request}
                }
                # this sends the request to our agent, which forwards it to Alice
                # (based on the connection_id)

                await agent.admin_POST(
                    "/present-proof-2.0/send-request", proof_request_web_request
                )

            elif option == "3":
                msg = await prompt("Enter message: ")
                await agent.admin_POST(
                    f"/connections/{agent.connection_id}/send-message", {"content": msg}
                )

        if show_timing:
            timing = await agent.fetch_timing()
            if timing:
                for line in agent.format_timing(timing):
                    log_msg(line)

    finally:
        terminated = True
        try:
            if agent:
                await agent.terminate()
        except Exception:
            LOGGER.exception("Error terminating agent:")
            terminated = False

    await asyncio.sleep(0.1)

    if not terminated:
        os._exit(1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Runs an Acme demo agent.")
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=8040,
        metavar=("<port>"),
        help="Choose the starting port number to listen on",
    )
    parser.add_argument(
        "--timing", action="store_true", help="Enable timing information"
    )
    args = parser.parse_args()

    require_indy()

    try:
        asyncio.get_event_loop().run_until_complete(main(args.port, args.timing))
    except KeyboardInterrupt:
        os._exit(1)
