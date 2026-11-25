#!/usr/bin/env python3
"""
CDR (Charging Data Record) Manager for CDF (Charging Data Function)
Compliant with 3GPP TS 32.240, TS 32.251, TS 32.260, and TS 32.299.

Handles charging session state, aggregates interim volume updates,
computes session durations, and generates standardized CDR files (JSON + CSV).
"""

import os
import json
import csv
import time
import logging
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple

from diameter_rf import (
    DiameterMessage,
    RECORD_TYPE_START,
    RECORD_TYPE_INTERIM,
    RECORD_TYPE_STOP,
    RECORD_TYPE_EVENT,
    RECORD_TYPE_NAMES,
    SUBSCRIPTION_ID_TYPE_IMSI,
    SUBSCRIPTION_ID_TYPE_E164,
)

logger = logging.getLogger("CDF.CDRManager")


class CDRManager:
    """Manages the creation, correlation, and export of Charging Data Records."""

    def __init__(self, cdr_dir: str = "cdrs"):
        self.cdr_dir = cdr_dir
        try:
            os.makedirs(self.cdr_dir, exist_ok=True)
        except OSError:
            pass
        self.csv_summary_path = os.path.join(self.cdr_dir, "cdrs_summary.csv")

        # In-memory tracking of active sessions: {session_id: session_data}
        self.active_sessions: Dict[str, Dict[str, Any]] = {}

        # Counters for monitoring
        self.total_sessions_started = 0
        self.total_sessions_closed = 0
        self.total_events_processed = 0
        self.total_octets_processed = 0

        self._init_csv()

    def _init_csv(self):
        """Initialize CSV summary file with standard 3GPP CDR fields if not existing."""
        if not os.path.exists(self.csv_summary_path):
            with open(self.csv_summary_path, mode="w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "Record_Sequence",
                    "Record_Type",
                    "Session_Id",
                    "IMSI",
                    "MSISDN",
                    "APN",
                    "Service_Context_Id",
                    "Start_Time",
                    "Stop_Time",
                    "Duration_Sec",
                    "Input_Octets",
                    "Output_Octets",
                    "Total_Octets",
                    "Cause_For_Closing",
                    "CTF_Host",
                    "Generated_At"
                ])

    def process_acr(self, acr: DiameterMessage) -> Dict[str, Any]:
        """
        Process an incoming Accounting-Request (ACR) from a CTF.
        Extracts 3GPP charging attributes and updates/finalizes CDRs.
        """
        sid_avp = acr.get_avp(263)
        session_id = sid_avp.as_str() if sid_avp else "UNKNOWN_SESSION"

        rec_type_avp = acr.get_avp(480)
        rec_type = rec_type_avp.as_u32() if rec_type_avp else RECORD_TYPE_EVENT

        rec_num_avp = acr.get_avp(485)
        rec_num = rec_num_avp.as_u32() if rec_num_avp else 0

        origin_host_avp = acr.get_avp(264)
        origin_host = origin_host_avp.as_str() if origin_host_avp else "unknown_ctf"

        service_ctx_avp = acr.get_avp(461)
        service_ctx = service_ctx_avp.as_str() if service_ctx_avp else "generic@3gpp.org"

        # Extract Subscriber Identifiers
        imsi, msisdn = self._extract_subscription_ids(acr)

        # Extract APN / Called-Station-Id
        apn_avp = acr.get_avp(30)
        apn = apn_avp.as_str() if apn_avp else "default.apn"

        # Extract Volume usage
        in_octets, out_octets, tot_octets = self._extract_octets(acr)

        now_ts = time.time()
        now_iso = datetime.fromtimestamp(now_ts).strftime("%Y-%m-%d %H:%M:%S")

        cdr_result: Dict[str, Any] = {}

        # ----------------------------------------------------------------------
        # 1. ACR [START]
        # ----------------------------------------------------------------------
        if rec_type == RECORD_TYPE_START:
            self.total_sessions_started += 1
            session_context = {
                "session_id": session_id,
                "record_type": "SESSION_PDU",
                "imsi": imsi,
                "msisdn": msisdn,
                "apn": apn,
                "service_context_id": service_ctx,
                "ctf_host": origin_host,
                "start_time_ts": now_ts,
                "start_time": now_iso,
                "last_update_ts": now_ts,
                "interim_records": [],
                "cumulative_in_octets": in_octets,
                "cumulative_out_octets": out_octets,
                "cumulative_total_octets": tot_octets,
                "state": "ACTIVE"
            }
            self.active_sessions[session_id] = session_context
            logger.info(
                f"[CDF] Session START registered: {session_id} | IMSI: {imsi} | APN: {apn}"
            )
            return {"status": "SESSION_INITIATED", "session_id": session_id}

        # ----------------------------------------------------------------------
        # 2. ACR [INTERIM]
        # ----------------------------------------------------------------------
        elif rec_type == RECORD_TYPE_INTERIM:
            session = self.active_sessions.get(session_id)
            if not session:
                session = {
                    "session_id": session_id,
                    "record_type": "SESSION_PDU",
                    "imsi": imsi,
                    "msisdn": msisdn,
                    "apn": apn,
                    "service_context_id": service_ctx,
                    "ctf_host": origin_host,
                    "start_time_ts": now_ts,
                    "start_time": now_iso,
                    "last_update_ts": now_ts,
                    "interim_records": [],
                    "cumulative_in_octets": 0,
                    "cumulative_out_octets": 0,
                    "cumulative_total_octets": 0,
                    "state": "ACTIVE"
                }
                self.active_sessions[session_id] = session

            interval_duration = round(now_ts - session["last_update_ts"], 2)
            session["last_update_ts"] = now_ts
            if in_octets > 0:
                session["cumulative_in_octets"] = in_octets if in_octets >= session["cumulative_in_octets"] else session["cumulative_in_octets"] + in_octets
            if out_octets > 0:
                session["cumulative_out_octets"] = out_octets if out_octets >= session["cumulative_out_octets"] else session["cumulative_out_octets"] + out_octets
            if tot_octets > 0:
                session["cumulative_total_octets"] = tot_octets
            else:
                session["cumulative_total_octets"] = session["cumulative_in_octets"] + session["cumulative_out_octets"]

            session["interim_records"].append({
                "record_number": rec_num,
                "timestamp": now_iso,
                "interval_duration_sec": interval_duration,
                "in_octets": in_octets,
                "out_octets": out_octets,
                "total_octets": session["cumulative_total_octets"]
            })
            logger.info(
                f"[CDF] Session INTERIM update: {session_id} | RecNum={rec_num} | "
                f"Vol={session['cumulative_total_octets']} B"
            )
            return {"status": "INTERIM_RECORDED", "session_id": session_id}

        # ----------------------------------------------------------------------
        # 3. ACR [STOP]
        # ----------------------------------------------------------------------
        elif rec_type == RECORD_TYPE_STOP:
            self.total_sessions_closed += 1
            session = self.active_sessions.pop(session_id, None)

            start_ts = session["start_time_ts"] if session else (now_ts - 5.0)
            start_iso = session["start_time"] if session else now_iso
            duration = max(1, round(now_ts - start_ts, 2))

            final_tot = tot_octets if tot_octets > 0 else (session["cumulative_total_octets"] if session else (in_octets + out_octets))
            final_in = in_octets if in_octets > 0 else (session["cumulative_in_octets"] if session else (final_tot // 3))
            final_out = out_octets if out_octets > 0 else (session["cumulative_out_octets"] if session else (final_tot - final_in))

            self.total_octets_processed += final_tot

            term_cause_avp = acr.get_avp(295)
            cause = "NormalRelease"
            if term_cause_avp:
                causes = {1: "DIAMETER_LOGOUT", 2: "DIAMETER_SERVICE_NOT_PROVIDED", 3: "DIAMETER_BAD_ANSWER"}
                cause = causes.get(term_cause_avp.as_u32(), f"Cause_{term_cause_avp.as_u32()}")

            cdr = {
                "cdr_id": f"CDR-SES-{int(now_ts)}-{session_id[-8:]}",
                "record_type": "SESSION_PDU_CHARGING_DATA_RECORD",
                "3gpp_spec": "3GPP TS 32.251 / TS 32.299",
                "session_id": session_id,
                "subscriber": {
                    "imsi": session["imsi"] if session else imsi,
                    "msisdn": session["msisdn"] if session else msisdn,
                },
                "network_element": {
                    "ctf_host": origin_host,
                    "apn_dnn": session["apn"] if session else apn,
                    "service_context_id": service_ctx,
                },
                "session_timing": {
                    "start_time": start_iso,
                    "stop_time": now_iso,
                    "duration_seconds": duration,
                },
                "traffic_volume": {
                    "uplink_input_octets": final_in,
                    "downlink_output_octets": final_out,
                    "total_volume_octets": final_tot,
                    "total_volume_mb": round(final_tot / (1024 * 1024), 3),
                },
                "closure_reason": cause,
                "interim_updates_count": len(session["interim_records"]) if session else 0,
                "generated_at": now_iso
            }

            self._save_cdr(cdr, is_event=False)
            logger.info(
                f"[CDF] Session STOP finalized: {session_id} | Duration: {duration}s | "
                f"Total Vol: {final_tot} B ({round(final_tot/(1024*1024),2)} MB)"
            )
            return cdr

        # ----------------------------------------------------------------------
        # 4. ACR [EVENT]
        # ----------------------------------------------------------------------
        elif rec_type == RECORD_TYPE_EVENT:
            self.total_events_processed += 1

            # Check for IMS / SMS calling/called addresses (direct or inside grouped Service-Info)
            calling_party = self._find_avp_value(acr, 831)
            called_party = self._find_avp_value(acr, 832)

            cdr = {
                "cdr_id": f"CDR-EVT-{int(now_ts)}-{session_id[-8:]}",
                "record_type": "EVENT_CHARGING_DATA_RECORD",
                "3gpp_spec": "3GPP TS 32.260 / TS 32.299",
                "session_id": session_id,
                "subscriber": {
                    "imsi": imsi,
                    "msisdn": msisdn,
                },
                "event_information": {
                    "service_context_id": service_ctx,
                    "calling_party": calling_party,
                    "called_party": called_party,
                    "event_type": "SMS_OR_VAS_EVENT",
                },
                "network_element": {
                    "ctf_host": origin_host,
                },
                "traffic_volume": {
                    "input_octets": in_octets,
                    "output_octets": out_octets,
                    "total_volume_octets": tot_octets,
                },
                "event_timestamp": now_iso,
                "generated_at": now_iso
            }

            self._save_cdr(cdr, is_event=True)
            logger.info(f"[CDF] EVENT CDR generated: {session_id} | IMSI: {imsi} | Ctx: {service_ctx}")
            return cdr

        return cdr_result

    def _find_avp_value(self, acr: DiameterMessage, code: int) -> Optional[str]:
        """Find AVP string value either top-level or inside grouped AVPs."""
        # Check top-level
        top = acr.get_avp(code)
        if top:
            return top.as_str()

        # Check grouped AVPs (e.g. Service-Information code 873)
        for g_code in (873, 876, 883, 443):
            g_avp = acr.get_avp(g_code)
            if g_avp:
                try:
                    inner_avps = g_avp.as_grouped()
                    for child in inner_avps:
                        if child.code == code:
                            return child.as_str()
                        # Level 2 nesting (e.g. Service-Info -> IMS-Info)
                        if child.code in (876, 883):
                            for sub_child in child.as_grouped():
                                if sub_child.code == code:
                                    return sub_child.as_str()
                except Exception:
                    pass
        return None

    def _extract_subscription_ids(self, acr: DiameterMessage) -> Tuple[str, str]:
        """Extract IMSI and MSISDN from Subscription-Id grouped AVPs."""
        imsi = "UNKNOWN_IMSI"
        msisdn = "UNKNOWN_MSISDN"

        # Check Subscription-Id AVPs (443)
        sub_avps = acr.get_avps(443)
        for sub_avp in sub_avps:
            try:
                inner = sub_avp.as_grouped()
                type_avp = next((a for a in inner if a.code == 450), None)
                data_avp = next((a for a in inner if a.code == 444), None)
                if type_avp and data_avp:
                    sub_type = type_avp.as_u32()
                    sub_data = data_avp.as_str()
                    if sub_type == SUBSCRIPTION_ID_TYPE_IMSI:
                        imsi = sub_data
                    elif sub_type == SUBSCRIPTION_ID_TYPE_E164:
                        msisdn = sub_data
            except Exception as e:
                logger.debug(f"Error parsing Subscription-Id: {e}")

        # Fallback to User-Name AVP (code 1)
        if msisdn == "UNKNOWN_MSISDN":
            uname_avp = acr.get_avp(1)
            if uname_avp:
                val = uname_avp.as_str().split("@")[0]
                msisdn = val

        return imsi, msisdn

    def _extract_octets(self, acr: DiameterMessage) -> Tuple[int, int, int]:
        """Extract input, output, and total octets from Used-Service-Unit or top-level AVPs."""
        in_oct = 0
        out_oct = 0
        tot_oct = 0

        # Check Used-Service-Unit (446)
        usu_avp = acr.get_avp(446)
        if usu_avp:
            try:
                inner = usu_avp.as_grouped()
                in_a = next((a for a in inner if a.code == 412), None)
                out_a = next((a for a in inner if a.code == 414), None)
                tot_a = next((a for a in inner if a.code == 421), None)
                if in_a:
                    in_oct = in_a.as_u64()
                if out_a:
                    out_oct = out_a.as_u64()
                if tot_a:
                    tot_oct = tot_a.as_u64()
            except Exception:
                pass

        # Check top-level octet AVPs
        if in_oct == 0:
            a = acr.get_avp(412)
            if a:
                in_oct = a.as_u64()
        if out_oct == 0:
            a = acr.get_avp(414)
            if a:
                out_oct = a.as_u64()
        if tot_oct == 0:
            a = acr.get_avp(421)
            if a:
                tot_oct = a.as_u64()

        if tot_oct == 0 and (in_oct + out_oct) > 0:
            tot_oct = in_oct + out_oct

        return in_oct, out_oct, tot_oct

    def _save_cdr(self, cdr: Dict[str, Any], is_event: bool = False):
        """Save CDR as JSON document and append summary line to CSV."""
        tag = "EVENT" if is_event else "SESSION"
        safe_sid = "".join(c if c.isalnum() else "_" for c in cdr["session_id"][-16:])
        ts_str = int(time.time())
        json_filename = f"CDR_{tag}_{ts_str}_{safe_sid}.json"
        json_path = os.path.join(self.cdr_dir, json_filename)

        # 1. Write formatted JSON
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(cdr, f, indent=2)

        # 2. Append to CSV
        sub = cdr.get("subscriber", {})
        net = cdr.get("network_element", {})
        timing = cdr.get("session_timing", {})
        traffic = cdr.get("traffic_volume", {})

        with open(self.csv_summary_path, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                cdr["cdr_id"],
                cdr["record_type"],
                cdr["session_id"],
                sub.get("imsi", "N/A"),
                sub.get("msisdn", "N/A"),
                net.get("apn_dnn", "N/A"),
                net.get("service_context_id", "N/A"),
                timing.get("start_time", cdr.get("event_timestamp", "N/A")),
                timing.get("stop_time", "N/A"),
                timing.get("duration_seconds", 0),
                traffic.get("uplink_input_octets", 0),
                traffic.get("downlink_output_octets", 0),
                traffic.get("total_volume_octets", 0),
                cdr.get("closure_reason", "EVENT_EXECUTED"),
                net.get("ctf_host", "N/A"),
                cdr["generated_at"]
            ])

    def get_stats(self) -> Dict[str, Any]:
        """Return real-time CDF statistics."""
        return {
            "active_sessions": len(self.active_sessions),
            "total_sessions_started": self.total_sessions_started,
            "total_sessions_closed": self.total_sessions_closed,
            "total_events_processed": self.total_events_processed,
            "total_octets_processed": self.total_octets_processed,
            "total_volume_mb": round(self.total_octets_processed / (1024 * 1024), 2),
        }
