"""Stress test for the RC1882CEF-MIOTY1 module, via the rc1882_mioty library.

Two phases:

1. Protocol stress — thousands of back-to-back non-RF AT commands (queries,
   config get/set, attach/detach-local) plus deliberate invalid-input fuzzing,
   to hammer the UART/AT parsing and error handling. No radio transmissions.

2. RF stress — repeated real over-the-air unidirectional sends, to exercise
   actual TX reliability and packet-counter behaviour. This keys up the radio
   on a shared ISM band, so transmissions are paced to stay under a
   conservative duty-cycle budget (default 0.1%, safe for every EU sub-band
   this module documents: EU0/EU1 are 1%, EU2 is 0.1%). The pacing interval is
   computed from a real measured TX duration, not guessed, and total RF
   runtime is capped unless you explicitly override it.

Example:
    python3 stress_test.py
    python3 stress_test.py --protocol-iterations 500 --tx-count 10
    python3 stress_test.py --skip-rf
"""

from __future__ import annotations

import argparse
import os
import random
import statistics
import time
from dataclasses import dataclass, field

import serial

from rc1882_mioty import (
    AttachState,
    MiotyCommandError,
    MiotyError,
    MiotyTimeoutError,
    RC1882Mioty,
    UplinkMode,
    UplinkProfile,
)

MAX_RF_RUNTIME_SECONDS = 20 * 60  # safety cap unless --force-tx-count is given


@dataclass
class ActionStats:
    name: str
    successes: int = 0
    expected_errors: int = 0  # ValueError / MiotyCommandError — the module or the
    #                            client correctly rejected something.
    unexpected_errors: int = 0  # MiotyTimeoutError or anything else — a real problem.
    latencies: list[float] = field(default_factory=list)

    def record(self, latency: float, outcome: str) -> None:
        self.latencies.append(latency)
        if outcome == "success":
            self.successes += 1
        elif outcome == "expected_error":
            self.expected_errors += 1
        else:
            self.unexpected_errors += 1

    def summary(self) -> str:
        total = self.successes + self.expected_errors + self.unexpected_errors
        if not self.latencies:
            return f"{self.name}: 0 calls"
        mean_ms = statistics.mean(self.latencies) * 1000
        max_ms = max(self.latencies) * 1000
        return (
            f"{self.name}: {total} calls | ok={self.successes} "
            f"expected_err={self.expected_errors} UNEXPECTED={self.unexpected_errors} "
            f"| latency mean={mean_ms:.1f}ms max={max_ms:.1f}ms"
        )


def _run_action(name: str, fn, stats: dict[str, ActionStats]) -> None:
    stat = stats.setdefault(name, ActionStats(name))
    start = time.perf_counter()
    try:
        fn()
        stat.record(time.perf_counter() - start, "success")
    except (ValueError, MiotyCommandError):
        stat.record(time.perf_counter() - start, "expected_error")
    except MiotyTimeoutError as e:
        stat.record(time.perf_counter() - start, "unexpected_error")
        print(f"  [UNEXPECTED] {name}: timeout — {e}")
    except MiotyError as e:
        stat.record(time.perf_counter() - start, "unexpected_error")
        print(f"  [UNEXPECTED] {name}: {e}")


def run_protocol_stress(
    modem: RC1882Mioty, iterations: int, gap: float = 0.0
) -> dict[str, ActionStats]:
    """Phase 1: rapid non-RF commands, including deliberate invalid inputs.

    `gap` is an optional fixed delay (seconds) inserted after every command,
    to test whether the module needs breathing room between back-to-back
    commands (a completely gapless run showed a high rate of dropped response
    fields and timeouts — see stress_test findings).
    """
    print(f"=== Protocol stress: {iterations} iterations, no RF, gap={gap * 1000:.0f}ms ===")
    stats: dict[str, ActionStats] = {}

    actions = [
        ("get_modem_info", lambda: modem.get_modem_info()),
        ("get_library_version", lambda: modem.get_library_version()),
        ("get_eui", lambda: modem.get_eui()),
        ("get_short_address", lambda: modem.get_short_address()),
        ("get_packet_counter", lambda: modem.get_packet_counter()),
        ("set_uplink_mode_valid", lambda: modem.set_uplink_mode(random.choice(list(UplinkMode)))),
        ("set_uplink_profile_valid", lambda: modem.set_uplink_profile(random.choice(list(UplinkProfile)))),
        ("set_uplink_tx_power_valid", lambda: modem.set_uplink_tx_power(random.randint(0, 14))),
        ("set_sync_burst", lambda: modem.set_sync_burst(random.choice([True, False]))),
        ("attach_local", lambda: modem.attach_local()),
        ("detach_local", lambda: modem.detach_local()),
        ("get_mac_header_response_flag", lambda: modem.get_mac_header_response_flag()),
        # Deliberately invalid inputs — should always land in ValueError (client-side)
        # or MiotyCommandError (device-side), never a crash or a timeout.
        ("set_uplink_mode_INVALID", lambda: modem.set_uplink_mode(random.choice([-1, 3, 5, 100]))),
        ("set_uplink_tx_power_INVALID", lambda: modem.set_uplink_tx_power(random.choice([-5, 15, 99]))),
        ("set_short_address_INVALID_len", lambda: modem.set_short_address(os.urandom(random.choice([0, 1, 3, 4])))),
    ]

    report_every = max(iterations // 10, 1)
    for i in range(iterations):
        name, fn = random.choice(actions)
        _run_action(name, fn, stats)
        if gap:
            time.sleep(gap)
        if (i + 1) % report_every == 0:
            print(f"  ...{i + 1}/{iterations}")

    # Leave known-good state behind for the RF phase.
    modem.set_uplink_profile(UplinkProfile.EU0)
    modem.set_uplink_mode(UplinkMode.STANDARD_TRANSMISSION)
    return stats


def run_rf_stress(
    modem: RC1882Mioty,
    tx_count: int,
    target_duty_cycle: float,
    force: bool,
    unrestricted: bool = False,
) -> tuple[dict[str, ActionStats], list[int]]:
    """Phase 2: real over-the-air unidirectional sends.

    By default, transmissions are paced to stay under `target_duty_cycle`
    (computed from a real measured TX duration, not guessed). If `unrestricted`
    is set, that pacing is skipped entirely and transmissions run back-to-back —
    only appropriate when you know the emissions can't reach or affect other
    spectrum users (e.g. a shielded RF-isolation chamber).
    """
    print(f"\n=== RF stress: up to {tx_count} transmissions ===")

    # Known-good, documented-safe config regardless of what phase 1 left behind.
    modem.set_uplink_profile(UplinkProfile.EU0)
    modem.set_uplink_mode(UplinkMode.STANDARD_TRANSMISSION)

    # Measure one real transmission to base pacing (if any) on actual hardware
    # timing rather than a guess. mioty's telegram splitting intentionally
    # spreads sub-packets over several seconds even for small payloads, so
    # this is typically a few seconds, not milliseconds.
    warm_up_payload = os.urandom(10)
    start = time.perf_counter()
    modem.send_unidirectional(warm_up_payload, timeout=15)
    tx_duration = time.perf_counter() - start
    print(f"Measured single TX duration: {tx_duration * 1000:.0f}ms")

    if unrestricted:
        print("RF pacing disabled (--unrestricted-rf): transmitting back-to-back.")
        interval = 0.0
        estimated_total = tx_count * tx_duration
    else:
        interval = max(1.0, tx_duration / target_duty_cycle)
        estimated_total = tx_count * interval
        if estimated_total > MAX_RF_RUNTIME_SECONDS and not force:
            capped_count = max(1, int(MAX_RF_RUNTIME_SECONDS / interval))
            print(
                f"Requested {tx_count} transmissions at {interval:.1f}s apart would take "
                f"{estimated_total / 60:.1f} min. Capping to {capped_count} transmissions "
                f"(~{MAX_RF_RUNTIME_SECONDS / 60:.0f} min) — pass --force-tx-count to override."
            )
            tx_count = capped_count
            estimated_total = tx_count * interval
        print(
            f"Target duty cycle: {target_duty_cycle * 100:.2f}% | "
            f"pacing interval: {interval:.1f}s | estimated run time: {estimated_total / 60:.1f} min"
        )

    stats: dict[str, ActionStats] = {}
    packet_counters: list[int] = []
    payload_sizes = [4, 10, 20]
    total_tx_time = 0.0

    if unrestricted:
        # One-off boundary test: an oversized payload should be cleanly rejected
        # (manual Table 6: -MNFO 5 "Buffer Size Insufficient" / 11 "Uplink
        # Packing Error"), not crash or hang. Only run when pacing is off — its
        # duration is unbounded and would blow an otherwise-planned duty-cycle
        # budget (a 200-byte payload took several times longer than a 10-byte
        # one when tried during development).
        _run_action(
            "send_unidirectional_OVERSIZED",
            lambda: modem.send_unidirectional(os.urandom(200), timeout=60),
            stats,
        )

    loop_start = time.perf_counter()
    for i in range(tx_count):
        size = payload_sizes[i % len(payload_sizes)]
        payload = os.urandom(size)
        start = time.perf_counter()
        try:
            result = modem.send_unidirectional(payload, timeout=15)
            elapsed = time.perf_counter() - start
            total_tx_time += elapsed
            stats.setdefault("send_unidirectional", ActionStats("send_unidirectional")).record(
                elapsed, "success"
            )
            if result.packet_counter is not None:
                packet_counters.append(result.packet_counter)
        except MiotyTimeoutError as e:
            elapsed = time.perf_counter() - start
            stats.setdefault("send_unidirectional", ActionStats("send_unidirectional")).record(
                elapsed, "unexpected_error"
            )
            print(f"  [UNEXPECTED] send_unidirectional: timeout — {e}")
        except MiotyCommandError as e:
            elapsed = time.perf_counter() - start
            stats.setdefault("send_unidirectional", ActionStats("send_unidirectional")).record(
                elapsed, "expected_error"
            )
            print(f"  send_unidirectional rejected: {e}")

        print(f"  [{i + 1}/{tx_count}] {size}B payload, {elapsed * 1000:.0f}ms")
        if i < tx_count - 1:
            time.sleep(max(0.0, interval - elapsed))

    actual_duration = time.perf_counter() - loop_start
    actual_duty_cycle = (total_tx_time / actual_duration * 100) if actual_duration else 0.0
    print(
        f"Actual on-air time: {total_tx_time:.2f}s over ~{actual_duration:.0f}s "
        f"=> ~{actual_duty_cycle:.3f}% duty cycle"
    )

    # Sanity-check: the packet counter should strictly increase with each
    # successful send — a gap or reset would indicate dropped/miscounted packets.
    non_monotonic = sum(
        1 for a, b in zip(packet_counters, packet_counters[1:]) if b <= a
    )
    if non_monotonic:
        print(f"  [UNEXPECTED] packet counter was non-monotonic {non_monotonic} time(s): {packet_counters}")
    else:
        print(f"  Packet counter increased monotonically across {len(packet_counters)} sends: OK")

    return stats, packet_counters


def print_report(protocol_stats: dict[str, ActionStats], rf_stats: dict[str, ActionStats]) -> None:
    print("\n=== Summary ===")
    print("-- Protocol stress --")
    for stat in protocol_stats.values():
        print(f"  {stat.summary()}")
    if rf_stats:
        print("-- RF stress --")
        for stat in rf_stats.values():
            print(f"  {stat.summary()}")

    total_unexpected = sum(s.unexpected_errors for s in {**protocol_stats, **rf_stats}.values())
    if total_unexpected:
        print(f"\n{total_unexpected} UNEXPECTED error(s) occurred — see [UNEXPECTED] lines above.")
    else:
        print("\nNo unexpected errors. All failures were either client-side validation")
        print("or the module cleanly reporting an error status.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--port", default="/dev/ttyUSB0")
    parser.add_argument("--protocol-iterations", type=int, default=3000)
    parser.add_argument(
        "--protocol-gap",
        type=float,
        default=0.0,
        help="Fixed delay in seconds inserted after every protocol-stress command (default: %(default)s)",
    )
    parser.add_argument("--tx-count", type=int, default=30)
    parser.add_argument(
        "--tx-duty-cycle",
        type=float,
        default=0.001,
        help="Target duty cycle for RF stress, e.g. 0.001 = 0.1%% (default: %(default)s)",
    )
    parser.add_argument("--skip-rf", action="store_true", help="Only run the protocol stress phase")
    parser.add_argument(
        "--force-tx-count",
        action="store_true",
        help="Do not cap --tx-count even if it implies a very long run",
    )
    parser.add_argument(
        "--unrestricted-rf",
        action="store_true",
        help=(
            "Disable duty-cycle pacing and transmit back-to-back. Only use this "
            "when the emissions genuinely cannot reach or affect other spectrum "
            "users (e.g. a shielded RF-isolation chamber) — otherwise leave "
            "pacing on."
        ),
    )
    parser.add_argument("--seed", type=int, default=None, help="Random seed, for reproducible fuzzing")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    protocol_stats: dict[str, ActionStats] = {}
    rf_stats: dict[str, ActionStats] = {}

    try:
        with RC1882Mioty(args.port) as modem:
            try:
                protocol_stats = run_protocol_stress(
                    modem, args.protocol_iterations, gap=args.protocol_gap
                )
                if not args.skip_rf:
                    rf_stats, _ = run_rf_stress(
                        modem,
                        args.tx_count,
                        args.tx_duty_cycle,
                        args.force_tx_count,
                        unrestricted=args.unrestricted_rf,
                    )
            except KeyboardInterrupt:
                print("\nInterrupted — printing partial results.")
    except serial.SerialException as e:
        print(f"Could not open {args.port}: {e}")
        return

    print_report(protocol_stats, rf_stats)


if __name__ == "__main__":
    main()
