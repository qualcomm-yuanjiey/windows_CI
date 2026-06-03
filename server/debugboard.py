#!/usr/bin/env python3

import argparse
import pexpect
import serial
import sys
import usb.core
import usb.util
import time

try:
    import TACDev
except:
    print("Error importing TACDev for target power interface control!!")

from pexpect import fdpexpect

class DebugBoard:

    def __init__(self):
        self.connection = None
        self.expect_connection = None
        self.control_message = None
        self.ack_message = None

    # open serial connection
    def open_connection(self, serial_port):
        self.connection = serial.Serial()
        self.connection.baudrate = 115200
        self.connection.port = serial_port
        try:
            self.connection.open()
        except serial.SerialException:
            print("Serial Exception")
            sys.exit(1)
        self.expect_connection = fdpexpect.fdspawn(self.connection)
        if not self.expect_connection.isalive():
            print("Expect connection not created")
            sys.exit(1)

    def close_connection(self):
        if self.connection and self.connection.is_open:
            self.connection.close()

    def send_message(self, message):
        if self.control_message is None or self.ack_message is None:
            raise NotImplementedError

        if self.expect_connection and self.expect_connection.isalive():
            self.expect_connection.send('\r')
            self.expect_connection.expect(self.control_message)

            self.expect_connection.send(message)
            self.expect_connection.expect(self.ack_message)
        else:
            print("No expect connection")
            sys.exit(1)

    def set_qdl(self, state=True):
        raise NotImplementedError

    def set_power(self, state=True):
        raise NotImplementedError

    def set_usb(self, state=True):
        raise NotImplementedError

class LinaroDebugBoard(DebugBoard):

    def __init__(self):
        super(LinaroDebugBoard, self).__init__()
        self.control_message = ">"
        self.ack_message = ">"

    # turn power on/off
    def set_power(self, state=True):
        # True = on
        # False = off
        send_char = "P"
        if not state:
            send_char = "p"
        self.send_message(f"{send_char}\r")

    # turn USB-C on/off (data connection to the host)
    def set_usb(self, state=True):
        # True = on
        # False = off
        send_char = "U"
        if not state:
            send_char = "u"
        self.send_message(f"{send_char}\r")

class Alpaca(DebugBoard):

    def __init__(self):
        super(Alpaca, self).__init__()
        self.control_message = "CMD"
        self.ack_message = "ok"

    # turn QDL on/off
    def set_qdl(self, state=True):
        # True = on
        # False = off
        send_char = 1
        if not state:
            send_char = 0
        self.send_message(f"gpio swdwnld {send_char}\r")

    # turn power on/off
    def set_power(self, state=True):
        # True = on
        # False = off
        send_char = 1
        if not state:
            send_char = 0
        self.send_message(f"devicePower {send_char}\r")

    # turn USB-C on/off (data connection to the host)
    def set_usb(self, state=True):
        # True = on
        # False = off
        send_char = 1
        if not state:
            send_char = 0
        self.send_message(f"usbDevicePower {send_char}\r")

class TacDebugBoard():
    def __init__(self):
        self.dev_handle = TACDev
        self.tac_handle = None

    def open_connection(self, serial=None):
        '''
        Check if the serial number is valid for tac board type,
        Open connection if device is found with the same serial numberp as passed in --serial flag
        '''
        if serial and "/" not in serial:
            serial_number = serial
        else:
            raise ValueError("Invalid serial number for board type tac. Please check the serial number!")

        #Proceed to open devices and match the serial numbers
        for i in range(0,self.dev_handle.GetDeviceCount()):
            self.tac_handle = self.dev_handle.GetDevice(i)
            print(f"Device with serial number: { self.tac_handle.SerialNumber() } ")
            if self.tac_handle.SerialNumber() == serial:
                print(f"Device with serial number: {serial} found!")
                self.tac_handle.Open()
                return True

        raise ValueError(f"Could not find the board with Serial number: {serial}\nPlease check the serial number!")

    def close_connection(self):
        self.tac_handle.Close()

    def set_power(self, state=True):
        #True = on
        #False = off
        if state:
            self.tac_handle.PowerOnButton()
            time.sleep(4)
        else:
            self.tac_handle.PowerOffButton()
            time.sleep(2)

    def set_qdl(self, edl_mode=True):
        if edl_mode:
            self.tac_handle.BootToEDLButton()
            time.sleep(2)

    def set_fastboot(self, state=True):
        if state:
            self.tac_handle.BootToFastBootButton()
            time.sleep(2)

    def set_usb(self, usb_state=True):
        '''
        Placeholder function to maintain backward compatibility with existing flow!
        '''
        pass

    def list_devices(self) -> list:
        """
        Return a list of all TAC devices visible to the system.
        Each entry is a dict: {index, serial_number, port_name}.
        Does not require open_connection() – scans all devices directly.
        """
        devices = []
        count = self.dev_handle.GetDeviceCount()
        for i in range(count):
            dev = self.dev_handle.GetDevice(i)
            if dev is None:
                continue
            try:
                serial = str(dev.SerialNumber()).strip()
            except Exception:
                serial = "N/A"
            try:
                port = str(dev.PortName()).strip()
            except Exception:
                port = "N/A"
            devices.append({"index": i, "serial_number": serial, "port_name": port})
        return devices

# find QDL device
def find_qdl():
    dev = usb.core.find(idVendor=0x05c6, idProduct=0x9008)

    if dev is None:
        return False
    return True

if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("--board", required=True, choices=["alpaca", "linaro","tac"])
    parser.add_argument("--serial", required=True, help="Path to serial connection device in case of alpaca and linaro board type, debugboard serial number if the board type is tac")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--set-boot-mode", choices=["EDL", "fastboot", "Normal"], help="Sets boot mode to either EDL, fastboot or Normal.")
    group.add_argument("--power-on", action="store_true", help="Powers the device ON")
    group.add_argument("--power-off", action="store_true", help="Powers the device OFF")

    args = parser.parse_args()

    a = None
    if args.board == "alpaca":
        a = Alpaca()
    if args.board == "linaro":
        a = LinaroDebugBoard()
    if args.board == "tac":
        a = TacDebugBoard()

    a.open_connection(args.serial)

    if args.power_on:
        a.set_power(True)
        a.set_usb(True)

    if args.power_off:
        a.set_power(False)
        a.set_usb(False)

    if args.set_boot_mode:
        if args.set_boot_mode == "EDL":
            a.set_qdl(True)
        if args.set_boot_mode == "fastboot":
            a.set_fastboot(True)
        else:
            a.set_qdl(False)
    a.close_connection()
