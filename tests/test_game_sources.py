from __future__ import annotations

import ctypes
import json
import struct
import threading
import unittest
import urllib.request

import ams2_bridge
import acevo_bridge
import game_sources


class GameSourceLayoutTests(unittest.TestCase):
    def test_kunos_gears_are_normalized(self) -> None:
        self.assertEqual(game_sources._kunos_gear(0), -1)
        self.assertEqual(game_sources._kunos_gear(1), 0)
        self.assertEqual(game_sources._kunos_gear(2), 1)
        self.assertEqual(game_sources._kunos_gear(8), 7)
        self.assertEqual(acevo_bridge._kunos_gear(0), -1)
        self.assertEqual(acevo_bridge._kunos_gear(2), 1)

    def test_ams2_does_not_guess_tc_from_car_flags(self) -> None:
        page = ams2_bridge.SharedMemoryPrefix()
        page.mVersion = 14
        page.mSequenceNumber = 2
        page.mGameState = 2
        page.mMaxRPM = 8000.0
        page.mRpm = 5000.0
        page.mCarFlags = 1 << 6
        reader = ams2_bridge.AMS2Reader()
        reader._kernel32 = object()
        reader._read_consistent = lambda: page
        self.assertFalse(reader.snapshot()["tcActive"])

    def test_supported_shared_memory_layout_sizes(self) -> None:
        self.assertEqual(ctypes.sizeof(ams2_bridge.SharedMemoryPrefix), 7324)
        self.assertEqual(ctypes.sizeof(game_sources._AcPhysics), 580)
        self.assertEqual(ctypes.sizeof(game_sources._AcGraphics), 296)
        self.assertEqual(ctypes.sizeof(game_sources._AcStatic), 684)
        self.assertEqual(ctypes.sizeof(game_sources._AccPhysics), 800)
        self.assertEqual(ctypes.sizeof(game_sources._AccGraphics), 1588)
        self.assertEqual(ctypes.sizeof(game_sources._AccStatic), 820)
        self.assertEqual(ctypes.sizeof(game_sources._LmuWheel), 260)
        self.assertEqual(ctypes.sizeof(game_sources._LmuTelemetry), 1888)
        self.assertEqual(ctypes.sizeof(game_sources._LmuObject), 324820)

    def test_forza_horizon_dash_offsets(self) -> None:
        packet = bytearray(324)
        struct.pack_into("<iIfff", packet, 0, 1, 42, 8000.0, 0.0, 6400.0)
        struct.pack_into("<f", packet, 84, 0.25)
        struct.pack_into("<f", packet, 256, 30.0)
        for offset in (268, 272, 276, 280):
            struct.pack_into("<f", packet, offset, 212.0)
        packet[315] = 128
        packet[316] = 64
        packet[317] = 32
        packet[319] = 4
        decoded = game_sources.ForzaUdpReader("fh5")._decode(bytes(packet))
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["version"], "FH4/FH5 Dash")
        self.assertAlmostEqual(decoded["speedKph"], 108.0)
        self.assertEqual(decoded["gear"], 4)
        self.assertAlmostEqual(decoded["tireTempMax"], 100.0)

    def test_forza_331_uses_v1_offsets(self) -> None:
        packet = bytearray(331)
        struct.pack_into("<iIfff", packet, 0, 1, 7, 9000.0, 0.0, 7200.0)
        struct.pack_into("<f", packet, 244, 20.0)
        packet[303] = 255
        packet[304] = 128
        packet[305] = 0
        packet[307] = 0xFF
        decoded = game_sources.ForzaUdpReader("fh6")._decode(bytes(packet))
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["version"], "Dash v3")
        self.assertAlmostEqual(decoded["speedKph"], 72.0)
        self.assertEqual(decoded["gear"], -1)


class BridgeHealthTests(unittest.TestCase):
    def test_health_identifies_the_exact_bridge_instance(self) -> None:
        handler = ams2_bridge.DemoHandler
        handler.root = str(__import__("pathlib").Path(__file__).resolve().parents[1])
        handler.bridge_token = "test-token"
        handler.bridge_version = "test-version"
        handler.parent_pid = 1234
        server = ams2_bridge.DemoServer(("127.0.0.1", 0), handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/api/health", timeout=2
            ) as response:
                payload = json.load(response)
            self.assertEqual(payload["service"], "k6-telemetry-bridge")
            self.assertEqual(payload["version"], "test-version")
            self.assertEqual(payload["token"], "test-token")
            self.assertEqual(payload["parentPid"], 1234)
            self.assertEqual(payload["root"], handler.root)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
