#!/usr/bin/env python3
"""
Diameter Protocol Codec for 3GPP Rf Interface (Offline Charging)
Based on RFC 6733 (Diameter Base) and 3GPP TS 32.299 / TS 32.240.

Provides binary serialization and deserialization of Diameter messages and AVPs
over TCP/SCTP for CTF and CDF simulation.
"""

import struct
import socket
import time
from typing import List, Dict, Any, Optional, Tuple, Union

# ==============================================================================
# Constants & Enums
# ==============================================================================

# Diameter Header Flag Bits
FLAG_REQUEST = 0x80      # 'R' bit: 1 = Request, 0 = Answer
FLAG_PROXIABLE = 0x40    # 'P' bit: 1 = Proxiable
FLAG_ERROR = 0x20        # 'E' bit: 1 = Protocol Error
FLAG_RETRANSMITTED = 0x10# 'T' bit: 1 = Potentially retransmitted

# AVP Flag Bits
AVP_FLAG_VENDOR = 0x80   # 'V' bit: 1 = Vendor-Id is present
AVP_FLAG_MANDATORY = 0x40# 'M' bit: 1 = Mandatory
AVP_FLAG_PROTECTED = 0x20# 'P' bit: 1 = Protected

# Application IDs
APP_ID_DIAMETER_BASE = 0
APP_ID_DIAMETER_ACCOUNTING = 3  # 3GPP Rf uses Acct-Application-Id 3

# Command Codes
CMD_CAPABILITIES_EXCHANGE = 257  # CER / CEA
CMD_DEVICE_WATCHDOG = 280        # DWR / DWA
CMD_DISCONNECT_PEER = 282        # DPR / DPA
CMD_ACCOUNTING = 271             # ACR / ACA (Rf Offline Charging)

# 3GPP Vendor ID
VENDOR_ID_3GPP = 10415

# Accounting-Record-Type (AVP Code 480)
RECORD_TYPE_START = 1
RECORD_TYPE_INTERIM = 2
RECORD_TYPE_STOP = 3
RECORD_TYPE_EVENT = 4

RECORD_TYPE_NAMES = {
    1: "START_RECORD",
    2: "INTERIM_RECORD",
    3: "STOP_RECORD",
    4: "EVENT_RECORD",
}

# Standard Result-Codes (RFC 6733 / 3588)
RESULT_DIAMETER_SUCCESS = 2001
RESULT_DIAMETER_COMMAND_UNSUPPORTED = 3001
RESULT_DIAMETER_REALM_NOT_SERVED = 3004
RESULT_DIAMETER_INVALID_AVP_VALUE = 5004
RESULT_DIAMETER_MISSING_AVP = 5005
RESULT_DIAMETER_UNABLE_TO_COMPLY = 5012

# Subscription-Id-Type (AVP Code 450)
SUBSCRIPTION_ID_TYPE_E164 = 0
SUBSCRIPTION_ID_TYPE_IMSI = 1
SUBSCRIPTION_ID_TYPE_SIP_URI = 2
SUBSCRIPTION_ID_TYPE_NAI = 3
SUBSCRIPTION_ID_TYPE_PRIVATE = 4

# NTP Epoch Offset for Diameter Time (seconds between 1900-01-01 and 1970-01-01)
NTP_OFFSET = 2208988800


# ==============================================================================
# AVP Class
# ==============================================================================

class AVP:
    """Represents a Diameter Attribute-Value Pair (AVP)."""

    def __init__(
        self,
        code: int,
        data: bytes,
        vendor_id: Optional[int] = None,
        mandatory: bool = True,
        protected: bool = False,
        name: Optional[str] = None
    ):
        self.code = code
        self.data = data
        self.vendor_id = vendor_id
        self.mandatory = mandatory
        self.protected = protected
        self.name = name or KNOWN_AVP_NAMES.get(code, f"AVP-{code}")

    @property
    def flags(self) -> int:
        flags = 0
        if self.vendor_id is not None:
            flags |= AVP_FLAG_VENDOR
        if self.mandatory:
            flags |= AVP_FLAG_MANDATORY
        if self.protected:
            flags |= AVP_FLAG_PROTECTED
        return flags

    def encode(self) -> bytes:
        """Encode AVP to wire format with 4-octet alignment padding."""
        header_len = 12 if self.vendor_id is not None else 8
        avp_len = header_len + len(self.data)

        # AVP Header: Code (4B) + Flags (1B) + Length (3B)
        hdr = struct.pack("!IB", self.code, self.flags)
        hdr += struct.pack("!I", avp_len)[1:]  # 3-byte length

        if self.vendor_id is not None:
            hdr += struct.pack("!I", self.vendor_id)

        raw = hdr + self.data
        pad_len = (4 - (len(raw) % 4)) % 4
        return raw + (b"\x00" * pad_len)

    @classmethod
    def decode(cls, data: bytes, offset: int = 0) -> Tuple["AVP", int]:
        """Decode AVP from binary buffer at given offset. Returns (AVP, next_offset)."""
        if len(data) - offset < 8:
            raise ValueError(f"Truncated AVP header at offset {offset}")

        code, flags = struct.unpack("!IB", data[offset:offset+5])
        length_bytes = b"\x00" + data[offset+5:offset+8]
        avp_length = struct.unpack("!I", length_bytes)[0]

        is_vendor = bool(flags & AVP_FLAG_VENDOR)
        is_mandatory = bool(flags & AVP_FLAG_MANDATORY)
        is_protected = bool(flags & AVP_FLAG_PROTECTED)

        header_len = 12 if is_vendor else 8
        if avp_length < header_len:
            raise ValueError(f"Invalid AVP length {avp_length} (header is {header_len})")

        vendor_id = None
        if is_vendor:
            if len(data) - offset < 12:
                raise ValueError("Truncated AVP vendor header")
            vendor_id = struct.unpack("!I", data[offset+8:offset+12])[0]

        data_start = offset + header_len
        data_end = offset + avp_length
        if len(data) < data_end:
            raise ValueError(f"Truncated AVP data (expected {avp_length} bytes)")

        avp_data = data[data_start:data_end]

        # Account for 4-byte padding in wire length
        pad_len = (4 - (avp_length % 4)) % 4
        next_offset = data_end + pad_len

        return cls(
            code=code,
            data=avp_data,
            vendor_id=vendor_id,
            mandatory=is_mandatory,
            protected=is_protected
        ), next_offset

    # Helper Decoders
    def as_u32(self) -> int:
        return struct.unpack("!I", self.data)[0]

    def as_i32(self) -> int:
        return struct.unpack("!i", self.data)[0]

    def as_u64(self) -> int:
        return struct.unpack("!Q", self.data)[0]

    def as_str(self) -> str:
        return self.data.decode("utf-8", errors="replace")

    def as_ip(self) -> str:
        if len(self.data) >= 6 and struct.unpack("!H", self.data[:2])[0] == 1:
            return socket.inet_ntoa(self.data[2:6])
        elif len(self.data) >= 18 and struct.unpack("!H", self.data[:2])[0] == 2:
            return socket.inet_ntop(socket.AF_INET6, self.data[2:18])
        return self.data.hex()

    def as_grouped(self) -> List["AVP"]:
        """Parse grouped AVP payload into inner AVPs."""
        avps = []
        off = 0
        while off < len(self.data):
            child_avp, off = AVP.decode(self.data, off)
            avps.append(child_avp)
        return avps

    def __repr__(self) -> str:
        vendor_str = f", vendor={self.vendor_id}" if self.vendor_id else ""
        return f"<AVP {self.name}({self.code}){vendor_str}: len={len(self.data)}>"


# ==============================================================================
# AVP Constructors
# ==============================================================================

def avp_u32(code: int, value: int, vendor_id: Optional[int] = None, mandatory: bool = True) -> AVP:
    return AVP(code, struct.pack("!I", value), vendor_id=vendor_id, mandatory=mandatory)

def avp_i32(code: int, value: int, vendor_id: Optional[int] = None, mandatory: bool = True) -> AVP:
    return AVP(code, struct.pack("!i", value), vendor_id=vendor_id, mandatory=mandatory)

def avp_u64(code: int, value: int, vendor_id: Optional[int] = None, mandatory: bool = True) -> AVP:
    return AVP(code, struct.pack("!Q", value), vendor_id=vendor_id, mandatory=mandatory)

def avp_str(code: int, value: str, vendor_id: Optional[int] = None, mandatory: bool = True) -> AVP:
    return AVP(code, value.encode("utf-8"), vendor_id=vendor_id, mandatory=mandatory)

def avp_bytes(code: int, value: bytes, vendor_id: Optional[int] = None, mandatory: bool = True) -> AVP:
    return AVP(code, value, vendor_id=vendor_id, mandatory=mandatory)

def avp_time(code: int, timestamp: Optional[float] = None, vendor_id: Optional[int] = None) -> AVP:
    t = int(timestamp if timestamp is not None else time.time())
    return AVP(code, struct.pack("!I", t + NTP_OFFSET), vendor_id=vendor_id)

def avp_ip(code: int, ip_str: str, vendor_id: Optional[int] = None) -> AVP:
    try:
        # IPv4
        ip_bytes = socket.inet_aton(ip_str)
        return AVP(code, struct.pack("!H", 1) + ip_bytes, vendor_id=vendor_id)
    except socket.error:
        # IPv6
        ip_bytes = socket.inet_pton(socket.AF_INET6, ip_str)
        return AVP(code, struct.pack("!H", 2) + ip_bytes, vendor_id=vendor_id)

def avp_grouped(code: int, inner_avps: List[AVP], vendor_id: Optional[int] = None, mandatory: bool = True) -> AVP:
    encoded_children = b"".join(a.encode() for a in inner_avps)
    return AVP(code, encoded_children, vendor_id=vendor_id, mandatory=mandatory)


# ==============================================================================
# Diameter Message Class
# ==============================================================================

class DiameterMessage:
    """Represents a complete Diameter RFC 6733 Command Message."""

    def __init__(
        self,
        command_code: int,
        application_id: int = APP_ID_DIAMETER_ACCOUNTING,
        is_request: bool = True,
        is_proxiable: bool = True,
        is_error: bool = False,
        is_retransmitted: bool = False,
        hop_by_hop_id: int = 1001,
        end_to_end_id: int = 2001,
        avps: Optional[List[AVP]] = None
    ):
        self.version = 1
        self.command_code = command_code
        self.application_id = application_id
        self.is_request = is_request
        self.is_proxiable = is_proxiable
        self.is_error = is_error
        self.is_retransmitted = is_retransmitted
        self.hop_by_hop_id = hop_by_hop_id
        self.end_to_end_id = end_to_end_id
        self.avps: List[AVP] = avps or []

    @property
    def flags(self) -> int:
        flags = 0
        if self.is_request:
            flags |= FLAG_REQUEST
        if self.is_proxiable:
            flags |= FLAG_PROXIABLE
        if self.is_error:
            flags |= FLAG_ERROR
        if self.is_retransmitted:
            flags |= FLAG_RETRANSMITTED
        return flags

    def get_avp(self, code: int, vendor_id: Optional[int] = None) -> Optional[AVP]:
        """Find first AVP matching code and optional vendor_id."""
        for a in self.avps:
            if a.code == code:
                if vendor_id is None or a.vendor_id == vendor_id:
                    return a
        return None

    def get_avps(self, code: int, vendor_id: Optional[int] = None) -> List[AVP]:
        """Find all AVPs matching code and optional vendor_id."""
        result = []
        for a in self.avps:
            if a.code == code:
                if vendor_id is None or a.vendor_id == vendor_id:
                    result.append(a)
        return result

    def add_avp(self, avp: AVP) -> "DiameterMessage":
        self.avps.append(avp)
        return self

    def encode(self) -> bytes:
        """Encode entire Diameter message into binary wire format."""
        encoded_avps = b"".join(a.encode() for a in self.avps)
        msg_length = 20 + len(encoded_avps)

        # Header (20 octets):
        # Version (1B) + Length (3B)
        hdr = struct.pack("!B", self.version)
        hdr += struct.pack("!I", msg_length)[1:]

        # Command Flags (1B) + Command Code (3B)
        hdr += struct.pack("!B", self.flags)
        hdr += struct.pack("!I", self.command_code)[1:]

        # Application-ID (4B) + Hop-by-Hop-ID (4B) + End-to-End-ID (4B)
        hdr += struct.pack("!III", self.application_id, self.hop_by_hop_id, self.end_to_end_id)

        return hdr + encoded_avps

    @classmethod
    def decode(cls, data: bytes) -> "DiameterMessage":
        """Decode complete Diameter message from wire buffer."""
        if len(data) < 20:
            raise ValueError(f"Data too short for Diameter header: {len(data)} bytes")

        version = data[0]
        if version != 1:
            raise ValueError(f"Unsupported Diameter version: {version}")

        msg_len = struct.unpack("!I", b"\x00" + data[1:4])[0]
        if len(data) < msg_len:
            raise ValueError(f"Buffer shorter ({len(data)}) than Diameter message length ({msg_len})")

        cmd_flags = data[4]
        is_request = bool(cmd_flags & FLAG_REQUEST)
        is_proxiable = bool(cmd_flags & FLAG_PROXIABLE)
        is_error = bool(cmd_flags & FLAG_ERROR)
        is_retransmitted = bool(cmd_flags & FLAG_RETRANSMITTED)

        command_code = struct.unpack("!I", b"\x00" + data[5:8])[0]
        app_id, hbh_id, ete_id = struct.unpack("!III", data[8:20])

        avps = []
        offset = 20
        while offset < msg_len:
            avp, offset = AVP.decode(data[:msg_len], offset)
            avps.append(avp)

        msg = cls(
            command_code=command_code,
            application_id=app_id,
            is_request=is_request,
            is_proxiable=is_proxiable,
            is_error=is_error,
            is_retransmitted=is_retransmitted,
            hop_by_hop_id=hbh_id,
            end_to_end_id=ete_id,
            avps=avps
        )
        return msg

    def get_command_name(self) -> str:
        cmd_names = {
            CMD_CAPABILITIES_EXCHANGE: ("CER" if self.is_request else "CEA"),
            CMD_DEVICE_WATCHDOG: ("DWR" if self.is_request else "DWA"),
            CMD_DISCONNECT_PEER: ("DPR" if self.is_request else "DPA"),
            CMD_ACCOUNTING: ("ACR" if self.is_request else "ACA"),
        }
        return cmd_names.get(self.command_code, f"CMD-{self.command_code}")

    def __repr__(self) -> str:
        name = self.get_command_name()
        return (
            f"<{name} AppId={self.application_id} HbH=0x{self.hop_by_hop_id:08x} "
            f"EtE=0x{self.end_to_end_id:08x} AVPs={len(self.avps)}>"
        )


# ==============================================================================
# Known AVP Names Mapping
# ==============================================================================

KNOWN_AVP_NAMES = {
    1: "User-Name",
    30: "Called-Station-Id",
    44: "Acct-Session-Id",
    50: "Acct-Multi-Session-Id",
    55: "Event-Timestamp",
    85: "Acct-Interim-Interval",
    257: "Host-IP-Address",
    258: "Auth-Application-Id",
    259: "Acct-Application-Id",
    263: "Session-Id",
    264: "Origin-Host",
    265: "Supported-Vendor-Id",
    266: "Vendor-Id",
    267: "Firmware-Revision",
    268: "Result-Code",
    269: "Product-Name",
    273: "Disconnect-Cause",
    278: "Origin-State-Id",
    283: "Destination-Realm",
    293: "Destination-Host",
    295: "Termination-Cause",
    296: "Origin-Realm",
    412: "CC-Input-Octets",
    414: "CC-Output-Octets",
    421: "CC-Total-Octets",
    432: "Rating-Group",
    439: "Service-Identifier",
    443: "Subscription-Id",
    444: "Subscription-Id-Data",
    446: "Used-Service-Unit",
    450: "Subscription-Id-Type",
    456: "Multiple-Services-Credit-Control",
    461: "Service-Context-Id",
    480: "Accounting-Record-Type",
    483: "Accounting-Realtime-Required",
    485: "Accounting-Record-Number",
    # 3GPP AVPs
    2: "3GPP-Charging-Id",
    3: "3GPP-PDP-Type",
    6: "3GPP-SGSN-Address",
    7: "3GPP-GGSN-Address",
    8: "3GPP-IMSI-MCC-MNC",
    9: "3GPP-GGSN-MCC-MNC",
    10: "3GPP-NSAPI",
    12: "3GPP-Selection-Mode",
    13: "3GPP-Charging-Characteristics",
    18: "3GPP-SGSN-MCC-MNC",
    21: "3GPP-RAT-Type",
    22: "3GPP-User-Location-Info",
    829: "Role-Of-Node",
    831: "Calling-Party-Address",
    832: "Called-Party-Address",
    861: "Cause-Code",
    862: "Node-Functionality",
    873: "Service-Information",
    874: "PS-Information",
    876: "IMS-Information",
    883: "SMS-Information",
}


# ==============================================================================
# Helper Message Builders for 3GPP Rf & Base Diameter
# ==============================================================================

def build_cer(
    origin_host: str = "ctf.rf.telecom.org",
    origin_realm: str = "rf.telecom.org",
    host_ip: str = "127.0.0.1",
    vendor_id: int = VENDOR_ID_3GPP,
    product_name: str = "CTF-Diameter-Client",
    hbh: int = 101,
    ete: int = 201
) -> DiameterMessage:
    """Build Capabilities-Exchange-Request (CER)."""
    msg = DiameterMessage(
        command_code=CMD_CAPABILITIES_EXCHANGE,
        application_id=APP_ID_DIAMETER_BASE,
        is_request=True,
        hop_by_hop_id=hbh,
        end_to_end_id=ete
    )
    msg.add_avp(avp_str(264, origin_host))
    msg.add_avp(avp_str(296, origin_realm))
    msg.add_avp(avp_ip(257, host_ip))
    msg.add_avp(avp_u32(266, vendor_id))
    msg.add_avp(avp_str(269, product_name))
    msg.add_avp(avp_u32(259, APP_ID_DIAMETER_ACCOUNTING))  # Acct-Application-Id
    return msg

def build_cea(
    cer_msg: DiameterMessage,
    origin_host: str = "cdf.rf.telecom.org",
    origin_realm: str = "rf.telecom.org",
    host_ip: str = "127.0.0.1",
    result_code: int = RESULT_DIAMETER_SUCCESS,
    vendor_id: int = VENDOR_ID_3GPP,
    product_name: str = "CDF-Diameter-Server"
) -> DiameterMessage:
    """Build Capabilities-Exchange-Answer (CEA) responding to CER."""
    msg = DiameterMessage(
        command_code=CMD_CAPABILITIES_EXCHANGE,
        application_id=APP_ID_DIAMETER_BASE,
        is_request=False,
        hop_by_hop_id=cer_msg.hop_by_hop_id,
        end_to_end_id=cer_msg.end_to_end_id
    )
    msg.add_avp(avp_u32(268, result_code))
    msg.add_avp(avp_str(264, origin_host))
    msg.add_avp(avp_str(296, origin_realm))
    msg.add_avp(avp_ip(257, host_ip))
    msg.add_avp(avp_u32(266, vendor_id))
    msg.add_avp(avp_str(269, product_name))
    msg.add_avp(avp_u32(259, APP_ID_DIAMETER_ACCOUNTING))
    return msg

def build_subscription_id(sub_type: int, sub_data: str) -> AVP:
    """Create Grouped Subscription-Id AVP."""
    return avp_grouped(443, [
        avp_u32(450, sub_type),
        avp_str(444, sub_data)
    ])

def build_ps_information(
    apn: str = "internet",
    charging_id: int = 10001,
    pdp_type: int = 0,
    pdp_address: str = "10.10.10.10",
    sgsn_address: str = "10.0.0.1",
    ggsn_address: str = "10.0.0.2"
) -> AVP:
    """Create 3GPP PS-Information Grouped AVP for Packet Switched charging."""
    inner = [
        avp_u32(2, charging_id, vendor_id=VENDOR_ID_3GPP),
        avp_u32(3, pdp_type, vendor_id=VENDOR_ID_3GPP),
        avp_ip(6, sgsn_address, vendor_id=VENDOR_ID_3GPP),
        avp_ip(7, ggsn_address, vendor_id=VENDOR_ID_3GPP),
        avp_str(30, apn)
    ]
    return avp_grouped(874, inner, vendor_id=VENDOR_ID_3GPP)

def build_used_service_unit(
    input_octets: int = 0,
    output_octets: int = 0,
    total_octets: Optional[int] = None
) -> AVP:
    """Create Used-Service-Unit Grouped AVP."""
    tot = total_octets if total_octets is not None else (input_octets + output_octets)
    return avp_grouped(446, [
        avp_u64(412, input_octets),
        avp_u64(414, output_octets),
        avp_u64(421, tot),
    ])

def build_acr(
    session_id: str,
    record_type: int,
    record_number: int,
    origin_host: str = "ctf.rf.telecom.org",
    origin_realm: str = "rf.telecom.org",
    destination_realm: str = "rf.telecom.org",
    service_context_id: str = "32251@3gpp.org",
    imsi: Optional[str] = None,
    msisdn: Optional[str] = None,
    apn: Optional[str] = None,
    input_octets: int = 0,
    output_octets: int = 0,
    total_octets: Optional[int] = None,
    calling_party: Optional[str] = None,
    called_party: Optional[str] = None,
    termination_cause: Optional[int] = None,
    hbh: int = 5001,
    ete: int = 6001
) -> DiameterMessage:
    """Build 3GPP Rf Accounting-Request (ACR)."""
    msg = DiameterMessage(
        command_code=CMD_ACCOUNTING,
        application_id=APP_ID_DIAMETER_ACCOUNTING,
        is_request=True,
        hop_by_hop_id=hbh,
        end_to_end_id=ete
    )
    # Mandatory AVPs per 3GPP TS 32.299
    msg.add_avp(avp_str(263, session_id))
    msg.add_avp(avp_str(264, origin_host))
    msg.add_avp(avp_str(296, origin_realm))
    msg.add_avp(avp_str(283, destination_realm))
    msg.add_avp(avp_u32(480, record_type))
    msg.add_avp(avp_u32(485, record_number))
    msg.add_avp(avp_u32(259, APP_ID_DIAMETER_ACCOUNTING))
    msg.add_avp(avp_str(461, service_context_id))
    msg.add_avp(avp_time(55))

    # Optional Subscription Identifiers
    if imsi:
        msg.add_avp(build_subscription_id(SUBSCRIPTION_ID_TYPE_IMSI, imsi))
    if msisdn:
        msg.add_avp(build_subscription_id(SUBSCRIPTION_ID_TYPE_E164, msisdn))
        msg.add_avp(avp_str(1, f"{msisdn}@{origin_realm}"))

    # Service / PS Information
    service_info_children = []
    if apn:
        msg.add_avp(avp_str(30, apn))
        ps_info = build_ps_information(apn=apn)
        service_info_children.append(ps_info)

    if calling_party or called_party:
        ims_info_children = []
        if calling_party:
            ims_info_children.append(avp_str(831, calling_party, vendor_id=VENDOR_ID_3GPP))
        if called_party:
            ims_info_children.append(avp_str(832, called_party, vendor_id=VENDOR_ID_3GPP))
        service_info_children.append(avp_grouped(876, ims_info_children, vendor_id=VENDOR_ID_3GPP))

    if service_info_children:
        msg.add_avp(avp_grouped(873, service_info_children, vendor_id=VENDOR_ID_3GPP))

    # Volume accounting
    if record_type in (RECORD_TYPE_INTERIM, RECORD_TYPE_STOP):
        msg.add_avp(build_used_service_unit(input_octets, output_octets, total_octets))
        if total_octets is not None:
            msg.add_avp(avp_u64(421, total_octets))

    # Termination Cause (for Stop)
    if record_type == RECORD_TYPE_STOP and termination_cause is not None:
        msg.add_avp(avp_u32(295, termination_cause))

    return msg

def build_aca(
    acr_msg: DiameterMessage,
    result_code: int = RESULT_DIAMETER_SUCCESS,
    origin_host: str = "cdf.rf.telecom.org",
    origin_realm: str = "rf.telecom.org"
) -> DiameterMessage:
    """Build 3GPP Rf Accounting-Answer (ACA) responding to ACR."""
    msg = DiameterMessage(
        command_code=CMD_ACCOUNTING,
        application_id=APP_ID_DIAMETER_ACCOUNTING,
        is_request=False,
        hop_by_hop_id=acr_msg.hop_by_hop_id,
        end_to_end_id=acr_msg.end_to_end_id
    )
    # Echo Session-Id
    sid = acr_msg.get_avp(263)
    if sid:
        msg.add_avp(sid)

    msg.add_avp(avp_u32(268, result_code))
    msg.add_avp(avp_str(264, origin_host))
    msg.add_avp(avp_str(296, origin_realm))
    msg.add_avp(avp_u32(259, APP_ID_DIAMETER_ACCOUNTING))

    # Echo Accounting-Record-Type & Record-Number
    rec_type = acr_msg.get_avp(480)
    if rec_type:
        msg.add_avp(rec_type)

    rec_num = acr_msg.get_avp(485)
    if rec_num:
        msg.add_avp(rec_num)

    return msg
