#!/usr/bin/env python3
"""
CDF (Charging Data Function) Server Simulation
3GPP Diameter Rf Offline Charging Server (RFC 6733 / 3GPP TS 32.299 / TS 32.240)

Listens on TCP for incoming Diameter Rf Accounting-Requests (ACR) from CTF clients,
validates charging parameters, correlates sessions, generates CDRs (JSON & CSV),
and responds with compliant Accounting-Answers (ACA).
"""

import sys
import socket
import struct
import threading
import argparse
import logging
import signal
import time
from typing import Optional

from diameter_rf import (
    DiameterMessage,
    build_cea,
    build_aca,
    avp_u32,
    avp_str,
    CMD_CAPABILITIES_EXCHANGE,
    CMD_DEVICE_WATCHDOG,
    CMD_DISCONNECT_PEER,
    CMD_ACCOUNTING,
    APP_ID_DIAMETER_BASE,
    APP_ID_DIAMETER_ACCOUNTING,
    RESULT_DIAMETER_SUCCESS,
    RESULT_DIAMETER_INVALID_AVP_VALUE,
    RESULT_DIAMETER_MISSING_AVP,
    RECORD_TYPE_NAMES,
)
from cdr_manager import CDRManager

# Setup Logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("CDF-Server")


class CDFServer:
    """Multi-threaded 3GPP Diameter Rf Offline Charging Data Function (CDF) Server."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 3868,
        origin_host: str = "cdf.rf.telecom.org",
        origin_realm: str = "rf.telecom.org",
        cdr_dir: str = "cdrs"
    ):
        self.host = host
        self.port = port
        self.origin_host = origin_host
        self.origin_realm = origin_realm
        self.cdr_manager = CDRManager(cdr_dir=cdr_dir)

        self._server_sock: Optional[socket.socket] = None
        self._running = False
        self._client_threads = []
        self._lock = threading.Lock()

    def start(self, blocking: bool = True):
        """Start the CDF TCP server."""
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            self._server_sock.bind((self.host, self.port))
            self._server_sock.listen(128)
            self._running = True
            logger.info("==================================================================")
            logger.info(f" 3GPP CDF (Charging Data Function) Server Started")
            logger.info(f" Listening on : {self.host}:{self.port} [TCP/Diameter]")
            logger.info(f" Origin-Host  : {self.origin_host}")
            logger.info(f" Origin-Realm : {self.origin_realm}")
            logger.info(f" CDR Output   : {self.cdr_manager.cdr_dir}/")
            logger.info("==================================================================")

            if blocking:
                self._accept_loop()
            else:
                accept_t = threading.Thread(target=self._accept_loop, daemon=True)
                accept_t.start()
        except Exception as e:
            logger.error(f"Failed to start CDF Server: {e}")
            self.stop()
            raise

    def _accept_loop(self):
        """Accept new CTF connections and dispatch to client handler threads."""
        while self._running and self._server_sock:
            try:
                client_sock, client_addr = self._server_sock.accept()
                logger.info(f"[CDF] Accepted CTF connection from {client_addr[0]}:{client_addr[1]}")
                t = threading.Thread(
                    target=self._handle_client,
                    args=(client_sock, client_addr),
                    daemon=True
                )
                with self._lock:
                    self._client_threads.append(t)
                t.start()
            except socket.error:
                break

    def _handle_client(self, sock: socket.socket, addr):
        """Handle Diameter protocol communication with a connected CTF peer."""
        sock.settimeout(120.0)
        buffer = bytearray()

        try:
            while self._running:
                # Read 4 bytes minimum to inspect Diameter message length
                chunk = sock.recv(4096)
                if not chunk:
                    logger.info(f"[CDF] CTF client {addr} disconnected.")
                    break
                buffer.extend(chunk)

                # Process all complete Diameter messages in buffer
                while len(buffer) >= 20:
                    # Message length is 24 bits stored in bytes 1-3
                    msg_length = struct.unpack("!I", b"\x00" + buffer[1:4])[0]
                    if len(buffer) < msg_length:
                        # Need more data
                        break

                    raw_msg = bytes(buffer[:msg_length])
                    del buffer[:msg_length]

                    # Parse message
                    try:
                        diameter_msg = DiameterMessage.decode(raw_msg)
                        self._dispatch_message(sock, diameter_msg, addr)
                    except Exception as e:
                        logger.error(f"[CDF] Error parsing message from {addr}: {e}")

        except socket.timeout:
            logger.warning(f"[CDF] Connection timeout with CTF client {addr}")
        except Exception as e:
            if self._running:
                logger.error(f"[CDF] Communication error with {addr}: {e}")
        finally:
            sock.close()

    def _dispatch_message(self, sock: socket.socket, msg: DiameterMessage, addr):
        """Process incoming Diameter command and send appropriate answer."""
        cmd_name = msg.get_command_name()

        # ----------------------------------------------------------------------
        # 1. Capabilities Exchange (CER)
        # ----------------------------------------------------------------------
        if msg.command_code == CMD_CAPABILITIES_EXCHANGE and msg.is_request:
            peer_host = msg.get_avp(264)
            peer_host_str = peer_host.as_str() if peer_host else str(addr)
            logger.info(f"[CDF] Received CER from CTF: {peer_host_str} (AppId={msg.application_id})")

            cea = build_cea(
                cer_msg=msg,
                origin_host=self.origin_host,
                origin_realm=self.origin_realm,
                host_ip=self.host if self.host != "0.0.0.0" else "127.0.0.1",
                result_code=RESULT_DIAMETER_SUCCESS,
                product_name="CDF-Charging-Data-Function"
            )
            sock.sendall(cea.encode())
            logger.info(f"[CDF] Sent CEA (Result-Code=2001 DIAMETER_SUCCESS) to {peer_host_str}")

        # ----------------------------------------------------------------------
        # 2. Accounting-Request (ACR - 3GPP Rf)
        # ----------------------------------------------------------------------
        elif msg.command_code == CMD_ACCOUNTING and msg.is_request:
            sid_avp = msg.get_avp(263)
            rec_type_avp = msg.get_avp(480)
            rec_num_avp = msg.get_avp(485)

            # Validate mandatory AVPs per 3GPP TS 32.299
            if not sid_avp or not rec_type_avp or not rec_num_avp:
                logger.warning(f"[CDF] ACR rejected: Missing mandatory AVPs from {addr}")
                err_aca = build_aca(
                    acr_msg=msg,
                    result_code=RESULT_DIAMETER_MISSING_AVP,
                    origin_host=self.origin_host,
                    origin_realm=self.origin_realm
                )
                sock.sendall(err_aca.encode())
                return

            rec_type = rec_type_avp.as_u32()
            rec_num = rec_num_avp.as_u32()
            rec_name = RECORD_TYPE_NAMES.get(rec_type, f"TYPE-{rec_type}")
            session_id = sid_avp.as_str()

            logger.info(
                f"[CDF <-- CTF] ACR [{rec_name}] | Session: {session_id} | RecordNum: {rec_num}"
            )

            # Process charging data in CDR Manager
            try:
                cdr_res = self.cdr_manager.process_acr(msg)
            except Exception as e:
                logger.error(f"[CDF] Error processing charging record: {e}")

            # Send Accounting-Answer (ACA)
            aca = build_aca(
                acr_msg=msg,
                result_code=RESULT_DIAMETER_SUCCESS,
                origin_host=self.origin_host,
                origin_realm=self.origin_realm
            )
            sock.sendall(aca.encode())
            logger.info(
                f"[CDF --> CTF] ACA [2001 SUCCESS] | Session: {session_id} | RecordNum: {rec_num}"
            )

        # ----------------------------------------------------------------------
        # 3. Device Watchdog (DWR) Keepalive
        # ----------------------------------------------------------------------
        elif msg.command_code == CMD_DEVICE_WATCHDOG and msg.is_request:
            dwa = DiameterMessage(
                command_code=CMD_DEVICE_WATCHDOG,
                application_id=APP_ID_DIAMETER_BASE,
                is_request=False,
                hop_by_hop_id=msg.hop_by_hop_id,
                end_to_end_id=msg.end_to_end_id
            )
            dwa.add_avp(avp_u32(268, RESULT_DIAMETER_SUCCESS))
            dwa.add_avp(avp_str(264, self.origin_host))
            dwa.add_avp(avp_str(296, self.origin_realm))
            sock.sendall(dwa.encode())
            logger.debug("[CDF] Responded to DWR keepalive.")

        # ----------------------------------------------------------------------
        # 4. Disconnect Peer (DPR)
        # ----------------------------------------------------------------------
        elif msg.command_code == CMD_DISCONNECT_PEER and msg.is_request:
            dpa = DiameterMessage(
                command_code=CMD_DISCONNECT_PEER,
                application_id=APP_ID_DIAMETER_BASE,
                is_request=False,
                hop_by_hop_id=msg.hop_by_hop_id,
                end_to_end_id=msg.end_to_end_id
            )
            dpa.add_avp(avp_u32(268, RESULT_DIAMETER_SUCCESS))
            dpa.add_avp(avp_str(264, self.origin_host))
            dpa.add_avp(avp_str(296, self.origin_realm))
            sock.sendall(dpa.encode())
            logger.info(f"[CDF] Disconnect Peer acknowledged for {addr}")

    def stop(self):
        """Stop server and clean up sockets."""
        self._running = False
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass
            self._server_sock = None
        logger.info("[CDF] Server stopped.")

    def print_summary(self):
        """Print server session and CDR metrics."""
        stats = self.cdr_manager.get_stats()
        print("\n" + "=" * 60)
        print(" CDF SERVER SESSION & CDR SUMMARY")
        print("=" * 60)
        print(f" Active Charging Sessions : {stats['active_sessions']}")
        print(f" Total Sessions Started   : {stats['total_sessions_started']}")
        print(f" Total Sessions Closed    : {stats['total_sessions_closed']}")
        print(f" Total Event CDRs Created : {stats['total_events_processed']}")
        print(f" Total Volume Accounted   : {stats['total_octets_processed']} Bytes ({stats['total_volume_mb']} MB)")
        print(f" CDR Output Location      : {self.cdr_manager.cdr_dir}/")
        print("=" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(description="3GPP Diameter Rf CDF (Charging Data Function) Server")
    parser.add_argument("--host", default="0.0.0.0", help="Listen IP (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=3868, help="Listen Port (default: 3868)")
    parser.add_argument("--origin-host", default="cdf.rf.telecom.org", help="Diameter Origin-Host")
    parser.add_argument("--origin-realm", default="rf.telecom.org", help="Diameter Origin-Realm")
    parser.add_argument("--cdr-dir", default="cdrs", help="Directory for generated CDRs (default: cdrs)")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose debug logging")
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    server = CDFServer(
        host=args.host,
        port=args.port,
        origin_host=args.origin_host,
        origin_realm=args.origin_realm,
        cdr_dir=args.cdr_dir
    )

    def sig_handler(sig, frame):
        logger.info("\nCaught termination signal. Shutting down CDF...")
        server.stop()
        server.print_summary()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    try:
        server.start(blocking=True)
    except KeyboardInterrupt:
        sig_handler(None, None)


if __name__ == "__main__":
    main()
