#!/usr/bin/env python3
"""
CTF (Charging Trigger Function) Client Simulation
3GPP Diameter Rf Offline Charging Client (RFC 6733 / 3GPP TS 32.299 / TS 32.240)

Simulates network elements (PGW, SMF, P-CSCF, SMS-C) generating Diameter Rf
Accounting-Requests (ACR) towards a CDF Server:
  - Session-Based Charging (SCUR): Start -> Interim -> Stop
  - Event-Based Charging (ECUR): Event (SMS, One-off charge)
  - Batch Multi-Subscriber Simulation: Reads subscriber dataset from CSV
"""

import sys
import os
import csv
import socket
import struct
import argparse
import logging
import time
from typing import Optional, List, Dict, Any

from diameter_rf import (
    DiameterMessage,
    build_cer,
    build_acr,
    CMD_CAPABILITIES_EXCHANGE,
    CMD_ACCOUNTING,
    RECORD_TYPE_START,
    RECORD_TYPE_INTERIM,
    RECORD_TYPE_STOP,
    RECORD_TYPE_EVENT,
    RECORD_TYPE_NAMES,
    RESULT_DIAMETER_SUCCESS,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [CTF] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("CTF-Client")


class CTFClient:
    """Simulates a Charging Trigger Function (CTF) over the 3GPP Rf interface."""

    def __init__(
        self,
        server_host: str = "127.0.0.1",
        server_port: int = 3868,
        origin_host: str = "ctf.rf.telecom.org",
        origin_realm: str = "rf.telecom.org",
        destination_realm: str = "rf.telecom.org"
    ):
        self.server_host = server_host
        self.server_port = server_port
        self.origin_host = origin_host
        self.origin_realm = origin_realm
        self.destination_realm = destination_realm
        self.sock: Optional[socket.socket] = None

        self._hbh_counter = 1000
        self._ete_counter = 2000

    def connect(self) -> bool:
        """Establish TCP connection and perform Capabilities Exchange (CER/CEA)."""
        logger.info(f"Connecting to CDF Server at {self.server_host}:{self.server_port}...")
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(10.0)
            self.sock.connect((self.server_host, self.server_port))
            logger.info("TCP Connection established.")

            # Send CER
            self._hbh_counter += 1
            self._ete_counter += 1
            cer = build_cer(
                origin_host=self.origin_host,
                origin_realm=self.origin_realm,
                host_ip=self.sock.getsockname()[0],
                hbh=self._hbh_counter,
                ete=self._ete_counter
            )
            self._send_msg(cer)
            logger.info(f"Sent CER (Origin-Host={self.origin_host})")

            # Receive CEA
            cea = self._recv_msg()
            res_code = cea.get_avp(268)
            code_val = res_code.as_u32() if res_code else 0
            if code_val == RESULT_DIAMETER_SUCCESS:
                peer_host = cea.get_avp(264)
                peer_str = peer_host.as_str() if peer_host else "CDF"
                logger.info(f"Received CEA [2001 DIAMETER_SUCCESS] from {peer_str}. Handshake complete!")
                return True
            else:
                logger.error(f"Capabilities exchange failed with Result-Code: {code_val}")
                return False

        except Exception as e:
            logger.error(f"Failed to connect to CDF: {e}")
            if self.sock:
                self.sock.close()
                self.sock = None
            return False

    def close(self):
        """Close TCP socket."""
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
            logger.info("CTF Connection closed.")

    def _send_msg(self, msg: DiameterMessage):
        data = msg.encode()
        self.sock.sendall(data)

    def _recv_msg(self) -> DiameterMessage:
        hdr = bytearray()
        while len(hdr) < 4:
            b = self.sock.recv(4 - len(hdr))
            if not b:
                raise ConnectionResetError("Connection lost while reading Diameter header")
            hdr.extend(b)

        msg_len = struct.unpack("!I", b"\x00" + hdr[1:4])[0]
        full_msg = bytearray(hdr)
        while len(full_msg) < msg_len:
            b = self.sock.recv(msg_len - len(full_msg))
            if not b:
                raise ConnectionResetError("Connection lost while reading Diameter payload")
            full_msg.extend(b)

        return DiameterMessage.decode(bytes(full_msg))

    def run_session_charging(
        self,
        session_id: str,
        imsi: str = "001010123456789",
        msisdn: str = "491701234567",
        apn: str = "internet",
        duration: int = 4,
        interim_interval: int = 2,
        total_volume_kb: int = 10240
    ) -> bool:
        """
        Simulate a Session-Based Charging flow (SCUR):
        1. ACR [START] (Record-Number = 0)
        2. ACR [INTERIM] at intervals
        3. ACR [STOP] (Record-Number = n)
        """
        logger.info(f"=== Starting Session Charging for Subscriber IMSI={imsi} | APN={apn} ===")
        record_number = 0
        total_bytes = total_volume_kb * 1024
        octets_per_sec = total_bytes // max(1, duration)

        # ----------------------------------------------------------------------
        # 1. ACR [START]
        # ----------------------------------------------------------------------
        self._hbh_counter += 1
        self._ete_counter += 1
        acr_start = build_acr(
            session_id=session_id,
            record_type=RECORD_TYPE_START,
            record_number=record_number,
            origin_host=self.origin_host,
            origin_realm=self.origin_realm,
            destination_realm=self.destination_realm,
            service_context_id="32251@3gpp.org",
            imsi=imsi,
            msisdn=msisdn,
            apn=apn,
            hbh=self._hbh_counter,
            ete=self._ete_counter
        )
        t0 = time.time()
        self._send_msg(acr_start)
        logger.info(f"[CTF --> CDF] ACR [START] | RecNum={record_number} | Session: {session_id}")

        aca_start = self._recv_msg()
        rtt = round((time.time() - t0) * 1000, 2)
        rc = aca_start.get_avp(268).as_u32() if aca_start.get_avp(268) else 0
        logger.info(f"[CTF <-- CDF] ACA [START] | Result-Code={rc} (RTT: {rtt}ms)")
        if rc != RESULT_DIAMETER_SUCCESS:
            logger.error(f"ACR [START] rejected with Result-Code {rc}")
            return False

        # ----------------------------------------------------------------------
        # 2. ACR [INTERIM] Loop
        # ----------------------------------------------------------------------
        elapsed = 0
        cumulative_bytes = 0
        while elapsed < duration:
            step = min(interim_interval, duration - elapsed)
            time.sleep(step)
            elapsed += step
            record_number += 1

            chunk_bytes = octets_per_sec * step
            cumulative_bytes += chunk_bytes
            in_bytes = chunk_bytes // 3
            out_bytes = chunk_bytes - in_bytes

            self._hbh_counter += 1
            self._ete_counter += 1
            acr_interim = build_acr(
                session_id=session_id,
                record_type=RECORD_TYPE_INTERIM,
                record_number=record_number,
                origin_host=self.origin_host,
                origin_realm=self.origin_realm,
                destination_realm=self.destination_realm,
                service_context_id="32251@3gpp.org",
                imsi=imsi,
                msisdn=msisdn,
                apn=apn,
                input_octets=in_bytes,
                output_octets=out_bytes,
                total_octets=cumulative_bytes,
                hbh=self._hbh_counter,
                ete=self._ete_counter
            )
            t0 = time.time()
            self._send_msg(acr_interim)
            logger.info(
                f"[CTF --> CDF] ACR [INTERIM] | RecNum={record_number} | "
                f"Vol={cumulative_bytes} B ({round(cumulative_bytes/1024,1)} KB)"
            )

            aca_interim = self._recv_msg()
            rtt = round((time.time() - t0) * 1000, 2)
            rc = aca_interim.get_avp(268).as_u32() if aca_interim.get_avp(268) else 0
            logger.info(f"[CTF <-- CDF] ACA [INTERIM] | Result-Code={rc} (RTT: {rtt}ms)")
            if rc != RESULT_DIAMETER_SUCCESS:
                logger.error(f"ACR [INTERIM] rejected with Result-Code {rc}")
                return False

        # ----------------------------------------------------------------------
        # 3. ACR [STOP]
        # ----------------------------------------------------------------------
        record_number += 1
        rem_bytes = max(0, total_bytes - cumulative_bytes)
        in_rem = rem_bytes // 3
        out_rem = rem_bytes - in_rem

        self._hbh_counter += 1
        self._ete_counter += 1
        acr_stop = build_acr(
            session_id=session_id,
            record_type=RECORD_TYPE_STOP,
            record_number=record_number,
            origin_host=self.origin_host,
            origin_realm=self.origin_realm,
            destination_realm=self.destination_realm,
            service_context_id="32251@3gpp.org",
            imsi=imsi,
            msisdn=msisdn,
            apn=apn,
            input_octets=in_rem,
            output_octets=out_rem,
            total_octets=total_bytes,
            termination_cause=1,  # DIAMETER_LOGOUT
            hbh=self._hbh_counter,
            ete=self._ete_counter
        )
        t0 = time.time()
        self._send_msg(acr_stop)
        logger.info(
            f"[CTF --> CDF] ACR [STOP] | RecNum={record_number} | "
            f"Final Total Vol={total_bytes} B ({round(total_bytes/(1024*1024),2)} MB)"
        )

        aca_stop = self._recv_msg()
        rtt = round((time.time() - t0) * 1000, 2)
        rc = aca_stop.get_avp(268).as_u32() if aca_stop.get_avp(268) else 0
        logger.info(f"[CTF <-- CDF] ACA [STOP] | Result-Code={rc} (RTT: {rtt}ms)")

        success = (rc == RESULT_DIAMETER_SUCCESS)
        if success:
            logger.info(f"=== Session Charging Complete for {session_id} ===")
        return success

    def run_event_charging(
        self,
        session_id: str,
        imsi: str = "001010123456789",
        msisdn: str = "491701234567",
        calling_party: str = "tel:+491701234567",
        called_party: str = "tel:+491709876543",
        service_context: str = "32260@3gpp.org"
    ) -> bool:
        """
        Simulate an Event-Based Charging flow (ECUR):
        Single ACR [EVENT] (Record-Number = 0)
        """
        logger.info(f"=== Starting Event Charging for SMS/VAS: {calling_party} -> {called_party} ===")
        self._hbh_counter += 1
        self._ete_counter += 1

        acr_event = build_acr(
            session_id=session_id,
            record_type=RECORD_TYPE_EVENT,
            record_number=0,
            origin_host=self.origin_host,
            origin_realm=self.origin_realm,
            destination_realm=self.destination_realm,
            service_context_id=service_context,
            imsi=imsi,
            msisdn=msisdn,
            calling_party=calling_party,
            called_party=called_party,
            hbh=self._hbh_counter,
            ete=self._ete_counter
        )

        t0 = time.time()
        self._send_msg(acr_event)
        logger.info(f"[CTF --> CDF] ACR [EVENT] | Session: {session_id} | Ctx: {service_context}")

        aca_event = self._recv_msg()
        rtt = round((time.time() - t0) * 1000, 2)
        rc = aca_event.get_avp(268).as_u32() if aca_event.get_avp(268) else 0
        logger.info(f"[CTF <-- CDF] ACA [EVENT] | Result-Code={rc} (RTT: {rtt}ms)")

        success = (rc == RESULT_DIAMETER_SUCCESS)
        if success:
            logger.info(f"=== Event Charging Complete for {session_id} ===")
        return success

    def run_batch_from_csv(self, csv_path: str):
        """Read subscribers CSV file and execute charging simulations for each subscriber."""
        if not os.path.exists(csv_path):
            logger.error(f"Subscribers CSV not found: {csv_path}")
            return

        logger.info(f"=== Loading Batch Test Data from {csv_path} ===")
        with open(csv_path, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                sid = f"ctf.rf.telecom.org;{int(time.time())};{row['SESSION_ID']}"
                imsi = row.get("IMSI", "001010123456789")
                msisdn = row.get("MSISDN", "491701234567")
                apn = row.get("APN", "internet")
                tot_oct = int(row.get("TOTAL_OCTETS", 1048576))

                logger.info(f"\n--- Batch Triggering: {imsi} | APN: {apn} | Vol: {tot_oct} bytes ---")
                self.run_session_charging(
                    session_id=sid,
                    imsi=imsi,
                    msisdn=msisdn,
                    apn=apn,
                    duration=2,
                    interim_interval=1,
                    total_volume_kb=tot_oct // 1024
                )
                time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser(description="3GPP Diameter Rf CTF (Charging Trigger Function) Client")
    parser.add_argument("--host", default="127.0.0.1", help="CDF Server IP (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=3868, help="CDF Server Port (default: 3868)")
    parser.add_argument("--mode", choices=["session", "event", "batch"], default="session", help="Simulation mode")
    parser.add_argument("--imsi", default="001010123456789", help="Subscriber IMSI")
    parser.add_argument("--msisdn", default="491701234567", help="Subscriber MSISDN")
    parser.add_argument("--apn", default="internet", help="APN / Called-Station-Id")
    parser.add_argument("--duration", type=int, default=3, help="Session duration in seconds")
    parser.add_argument("--interim-interval", type=int, default=1, help="Interval between ACR [Interim] in seconds")
    parser.add_argument("--volume-mb", type=int, default=10, help="Total data volume in MB")
    parser.add_argument("--csv", default="seagull/data/subscribers.csv", help="Subscribers CSV path for batch mode")
    args = parser.parse_args()

    client = CTFClient(server_host=args.host, server_port=args.port)
    if not client.connect():
        sys.exit(1)

    try:
        session_id = f"ctf.rf.telecom.org;{int(time.time())};{os.getpid()}"
        if args.mode == "session":
            client.run_session_charging(
                session_id=session_id,
                imsi=args.imsi,
                msisdn=args.msisdn,
                apn=args.apn,
                duration=args.duration,
                interim_interval=args.interim_interval,
                total_volume_kb=args.volume_mb * 1024
            )
        elif args.mode == "event":
            client.run_event_charging(
                session_id=session_id,
                imsi=args.imsi,
                msisdn=args.msisdn
            )
        elif args.mode == "batch":
            client.run_batch_from_csv(args.csv)
    finally:
        client.close()


if __name__ == "__main__":
    main()
