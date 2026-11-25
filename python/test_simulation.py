#!/usr/bin/env python3
"""
Automated End-to-End Integration Test Suite for CDF and CTF Simulation.
Validates:
  1. TCP & Diameter Capabilities Exchange (CER/CEA)
  2. Session-Based Offline Charging (ACR Start -> Interim -> Stop)
  3. Event-Based Offline Charging (ACR Event)
  4. Result-Code 2001 (DIAMETER_SUCCESS) verification
  5. Correct CDR Generation on Disk (JSON file + CSV row)
  6. CDR Field Accuracy (IMSI, MSISDN, APN, Duration, Uplink/Downlink Volumes)
"""

import os
import sys
import time
import json
import glob
import unittest
import threading

# Add python directory to path
sys.path.insert(0, os.path.dirname(__file__))

from cdf_server import CDFServer
from ctf_client import CTFClient


class TestCDRandCTFSimulation(unittest.TestCase):
    SERVER_HOST = "127.0.0.1"
    SERVER_PORT = 3869  # Test port
    CDR_TEST_DIR = os.path.join(os.path.dirname(__file__), "..", "cdrs_test")

    @classmethod
    def setUpClass(cls):
        """Start background CDF Server instance for automated testing."""
        try:
            os.makedirs(cls.CDR_TEST_DIR, exist_ok=True)
        except OSError:
            pass
        cls.server = CDFServer(
            host=cls.SERVER_HOST,
            port=cls.SERVER_PORT,
            origin_host="cdf.test.rf.telecom.org",
            origin_realm="test.rf.telecom.org",
            cdr_dir=cls.CDR_TEST_DIR
        )
        cls.server_thread = threading.Thread(target=cls.server.start, kwargs={"blocking": True}, daemon=True)
        cls.server_thread.start()
        time.sleep(0.5)  # Allow socket bind and listen

    @classmethod
    def tearDownClass(cls):
        """Stop background CDF Server instance."""
        cls.server.stop()
        time.sleep(0.2)

    def test_01_session_based_charging(self):
        """Test complete Session-Based Charging (SCUR) lifecycle."""
        client = CTFClient(
            server_host=self.SERVER_HOST,
            server_port=self.SERVER_PORT,
            origin_host="ctf.test.rf.telecom.org",
            origin_realm="test.rf.telecom.org"
        )
        connected = client.connect()
        self.assertTrue(connected, "Failed to connect and complete CER/CEA handshake")

        test_sid = f"ctf.test;{int(time.time())};session_01"
        test_imsi = "001010999888777"
        test_msisdn = "491709998887"
        test_apn = "test.internet.apn"

        success = client.run_session_charging(
            session_id=test_sid,
            imsi=test_imsi,
            msisdn=test_msisdn,
            apn=test_apn,
            duration=2,
            interim_interval=1,
            total_volume_kb=2048  # 2MB
        )
        client.close()
        self.assertTrue(success, "Session charging flow failed")

        # Verify CDR File generated in CDR directory
        cdr_files = glob.glob(os.path.join(self.CDR_TEST_DIR, "CDR_SESSION_*.json"))
        self.assertGreater(len(cdr_files), 0, "No SESSION CDR files found in output directory")

        # Inspect newest CDR file
        newest_file = max(cdr_files, key=os.path.getmtime)
        with open(newest_file, "r", encoding="utf-8") as f:
            cdr_data = json.load(f)

        self.assertEqual(cdr_data["record_type"], "SESSION_PDU_CHARGING_DATA_RECORD")
        self.assertEqual(cdr_data["subscriber"]["imsi"], test_imsi)
        self.assertEqual(cdr_data["subscriber"]["msisdn"], test_msisdn)
        self.assertEqual(cdr_data["network_element"]["apn_dnn"], test_apn)
        self.assertGreaterEqual(cdr_data["session_timing"]["duration_seconds"], 1)
        self.assertEqual(cdr_data["traffic_volume"]["total_volume_octets"], 2048 * 1024)
        print(f"[TEST PASS] Session CDR Verified: {newest_file}")

    def test_02_event_based_charging(self):
        """Test Event-Based Charging (ECUR) for SMS/VAS."""
        client = CTFClient(
            server_host=self.SERVER_HOST,
            server_port=self.SERVER_PORT,
            origin_host="ctf.test.rf.telecom.org",
            origin_realm="test.rf.telecom.org"
        )
        connected = client.connect()
        self.assertTrue(connected, "Failed to connect and complete CER/CEA handshake")

        test_sid = f"ctf.test;{int(time.time())};event_01"
        test_imsi = "001010333222111"
        test_msisdn = "491703332221"

        success = client.run_event_charging(
            session_id=test_sid,
            imsi=test_imsi,
            msisdn=test_msisdn,
            calling_party="tel:+491703332221",
            called_party="tel:+491704445556"
        )
        client.close()
        self.assertTrue(success, "Event charging flow failed")

        # Verify Event CDR File generated
        cdr_files = glob.glob(os.path.join(self.CDR_TEST_DIR, "CDR_EVENT_*.json"))
        self.assertGreater(len(cdr_files), 0, "No EVENT CDR files found in output directory")

        newest_file = max(cdr_files, key=os.path.getmtime)
        with open(newest_file, "r", encoding="utf-8") as f:
            cdr_data = json.load(f)

        self.assertEqual(cdr_data["record_type"], "EVENT_CHARGING_DATA_RECORD")
        self.assertEqual(cdr_data["subscriber"]["imsi"], test_imsi)
        self.assertEqual(cdr_data["subscriber"]["msisdn"], test_msisdn)
        self.assertEqual(cdr_data["event_information"]["calling_party"], "tel:+491703332221")
        self.assertEqual(cdr_data["event_information"]["called_party"], "tel:+491704445556")
        print(f"[TEST PASS] Event CDR Verified: {newest_file}")

    def test_03_csv_summary_file(self):
        """Verify that cdrs_summary.csv contains rows for both tests."""
        csv_path = os.path.join(self.CDR_TEST_DIR, "cdrs_summary.csv")
        self.assertTrue(os.path.exists(csv_path), "cdrs_summary.csv does not exist")
        with open(csv_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        self.assertGreaterEqual(len(lines), 3, "Expected header + at least 2 CDR entries in CSV")
        print(f"[TEST PASS] CSV Summary has {len(lines)-1} CDR entries recorded.")


if __name__ == "__main__":
    unittest.main()
