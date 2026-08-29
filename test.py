"""Interactive playground for the rc1882_mioty library.

Runs a handful of AT commands against a connected RC1882CEF-MIOTY1 module so
you can see the driver working end-to-end. Comment sections in/out below to
try different calls.

Example:
    python3 test.py
    python3 test.py --port /dev/ttyUSB1
"""

import argparse

import serial

from rc1882_mioty import (
    MiotyCommandError,
    MiotyTimeoutError,
    RC1882Mioty,
    UplinkMode,
    UplinkProfile,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--port",
        default="/dev/ttyUSB0",
        help="Serial port the module is connected to (default: %(default)s)",
    )
    args = parser.parse_args()

    try:
        with RC1882Mioty(args.port) as modem:
            print("=== Identification ===")
            info = modem.get_modem_info()
            print(f"Modem info:      {info}")
            lib = modem.get_library_version()
            print(f"Library version: {lib}")
            print(f"EUI64:           {modem.get_eui().hex().upper()}")
            print(f"Short address:   {modem.get_short_address().hex().upper()}")
            print(f"Packet counter:  {modem.get_packet_counter()}")

            print("\n=== Send a unidirectional message ===")
            result = modem.send_unidirectional(b"HelloWorld")
            print(f"Sent b'HelloWorld' -> {result}")

            print("\n=== Radio configuration ===")
            mode = modem.set_uplink_mode(UplinkMode.STANDARD_TRANSMISSION)
            print(f"Uplink mode confirmed:    {mode.name}")
            profile = modem.set_uplink_profile(UplinkProfile.EU0)
            print(f"Uplink profile confirmed: {profile.name}")
            power = modem.set_uplink_tx_power(14)
            print(f"TX power confirmed:       {power} dBm")

            print("\n=== Error handling ===")
            try:
                modem.set_uplink_tx_power(99)
            except ValueError as e:
                print(f"Caught expected validation error: {e}")

            try:
                modem.set_uplink_mode(5)
            except MiotyCommandError as e:
                print(f"Caught expected command error:    {e}")

            print("\n=== Bidirectional send ===")
            print("(this will likely time out unless a mioty base station is nearby)")
            try:
                downlink = modem.send_bidirectional(b"Ping", timeout=10)
                print(f"Downlink result: {downlink}")
            except MiotyTimeoutError as e:
                print(f"No response received (expected without a base station): {e}")

    except serial.SerialException as e:
        print(f"Could not open {args.port}: {e}")
        print("Check the module is connected and you're in the 'dialout' group.")


if __name__ == "__main__":
    main()
