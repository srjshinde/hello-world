import copy
import datetime
import json
import msgpack
import os
from pathlib import Path
import subprocess
import socket
from shutil import which
import time

import db
import display as dsp

EVENT_VERSION = 1
EVENT_TYPE_GUEST_REG = 2
EVENT_TYPE_MEM_GUEST_DECL = 3
EVENT_TYPE_REMOTE_ACTIVITY = 25
INSTALLATION_MODE_SENTINEL = "/run/installation_mode"

# get the socket file
socket_address = os.environ['PUSH_ADDR']
if socket_address == '':
    print("Missing env variable PUSH_ADDR")
    exit(-1)

# Custom error codes
class InvalidRC5Command(RuntimeError):
    pass


VERBOSE = False
DISPLAY_TIMEOUT=20
INFO_REFRESH_TIMEOUT=5
GREG_KP_TIMEOUT=20
MAX_ALLOWED_BRIGHTNESS=255
MIN_ALLOWED_BRIGHTNESS=1
BRIGHTNESS_LEVEL_STEP=20

def dprint(msg: str):
    if VERBOSE:
        print(msg)


class Guest():

    def __init__(self, position, identity=None):
        self.position = str(position)
        self.identity = identity


    def __repr__(self):
        return f"(G{self.position}, {self.identity})"


class State():

    def __init__(self):
        self.viewersDeclared = []
        self.viewersRegistered = []
        self.guestsRegistered = []
        self.absent = None
        self.cleared_aud = None
        self.grKeyPressTime = None
        self.toBeRegisteredGuest = None
        self.guestFlowKeys = None
        self.tv = None
        self.validKeys = None
        self.stateChangedAt = None
        self.wm_status = False
        self.gsm_status = False
        self.uploader_status = False
        self.brightnessLevel = 255
        self.in_installation_mode = False
        self.remote_paired = False
        self.is_bm3 = 40000000 > int(subprocess.getoutput("meter_id")) >= 30000000


class Remote(State):

    def declareStateVars(self):
        # variables for state refs
        self.guestRegState1 = ["GUEST"]
        self.guestRegState2 = ["G1", "G2", "G3", "G4", "G5"]
        self.guestRegState3 = ["M1", "M2", "M3", "M4", "M5", "F1", "F2", "F3", "F4", "F5", "OK"]
        self.lastRemoteCmd  = {'toggle':'', 'cmd':''}
        self.viewers        = ['A' , 'B' , 'C' , 'D' , 'E' , 'F' , 'G' , 'H' , 'I' , 'J' , 'K' ,
                               'L' , 'G1', 'G2', 'G3', 'G4', 'G5',]
        self.AgeGroup       = {
                                '1': " 4-14",
                                '2': "15-24",
                                '3': "25-34",
                                '4': "35-44",
                                '5': "45+  ",
                                ' ': "     ", # To handle the `guestRegState2` screen
                              }


    def declareKeyMaps(self):
        # Static mapping of the remote-keys to the RC5 code they generate
        self.KeyToNum = {
            'A': 18,
            'B': 19,
            'C': 2,
            'D': 6,
            'E': 0,
            'F': 35,
            'G': 41,
            'H': 44,
            'I': 1,
            'J': 5,
            'K': 7,
            'L': 9,
            'G1': 30,
            'G2': 36,
            'G3': 4,
            'G4': 8,
            'G5': 15,
            'M1': 17,
            'M2': 20,
            'M3': 21,
            'M4': 22,
            'M5': 23,
            'F1': 24,
            'F2': 25,
            'F3': 26,
            'F4': 27,
            'F5': 28,
            'ABS': 10,
            'GUEST': 45,
            'OK': 12,
            'CANCEL': 63,
            'INFO': 3,
            'INCB': 43,
            'DECB': 42,
        }

        self.NumToKey = {v: k for k, v in self.KeyToNum.items()}


    def loadGuestRegistration(self):
        """
        Get registered guests from DB.
        """
        self.guestsRegistered = []
        gr = self.dbi.loadGuestRegistration()
        for g in gr:
            self.guestsRegistered.append(Guest(g[0], g[1]))


    def loadDeclaration(self):
        """
        Get declared viewers from DB
        """
        self.viewersDeclared = []
        vd = self.dbi.loadDeclaration()
        for v in vd:
            if len(v) == 1 and v in self.viewersRegistered:
                self.viewersDeclared.append(v)
            elif len(v) == 2 and self.guest_reg(Guest(v[1:])):
                self.viewersDeclared.append(v)
        self.viewersDeclared.sort()


    def defaultRegMembers(self):
        """
        Default registered members during installation mode
        """
        return [chr(65+i) for i in range(12)]


    def readMemberConfig(self):
        """
        Get member config from OS
        """

        regs = []
        member_info = subprocess.getoutput('get_config MEMBER_INFO')
        if not member_info:
            if self.in_installation_mode:
                return self.defaultRegMembers() 
            return regs

        member_info = json.loads(member_info)
        if not member_info:
            if self.in_installation_mode:
                return self.defaultRegMembers()
            return regs

        member_pos = sorted([int(k[1:]) for k in member_info.keys()])
        regs = [chr(64+p) for p in member_pos]
        return regs


    def __init__(self):
        super().__init__()
        self.declareStateVars()
        self.declareKeyMaps()
        self.dbi = db.DBInterface()
        self.cleared_aud = self.dbi.loadClearedAud()
        self.viewersRegistered  = self.readMemberConfig()
        self.loadGuestRegistration()
        self.loadDeclaration()
        self.absent = self.dbi.getAbsentStatus()
        self.tv = self.dbi.loadTVState()
        self.validKeys = self.KeyToNum.keys()
        self.lastCommState = State()
        self.brightnessLevel = self.dbi.loadBrightnessLevel()
        self.in_installation_mode = self.dbi.loadInstallationModeState()
        self.refreshed_info_at = None
        self.last_known_key_press = None


    def dbusNotify(self):
        subprocess.call(
            "dbus-send --system /in/fluctus/baro3/DisplayHandler in.fluctus.baro3.DisplayHandler.StateChange",
            shell=True
        )


    def saveState(self):
        self.dprintStates("Saving states")
        self.dbi.saveState(self.dbi.viewershipConn, 'declared_viewers', json.dumps(self.viewersDeclared))
        self.dbi.saveState(self.dbi.viewershipConn, 'last_known_tv_state', int(self.tv))
        self.dbi.saveState(self.dbi.guestRegistrationConn, 'guests_registered', json.dumps([(g.position, g.identity) for g in self.guestsRegistered]))
        self.dbi.saveState(self.dbi.guestRegistrationConn, 'cleared_for_aud', self.cleared_aud)
        self.dbi.saveState(self.dbi.guestRegistrationConn, 'absent', int(self.absent))
        self.dbi.saveState(self.dbi.guestRegistrationConn, 'brightness_level', str(self.brightnessLevel))
        self.dbi.saveState(self.dbi.guestRegistrationConn, 'in_installation_mode', str(self.in_installation_mode))
        self.dbusNotify()
        self.stateChangedAt=None


    def clearViewership(self):
        dprint("Clearing viewership")
        self.viewersDeclared = []
        self.saveState()


    def clearGuestRegistration(self):
        dprint("Deregistering guests")
        for g in self.guestsRegistered:
            if "G"+g.position in self.viewersDeclared:
                self.viewersDeclared.remove("G"+g.position)
        self.viewersDeclared.sort()
        self.pushEvent()
        for g in self.guestsRegistered:
            self.pushEvent(deReg=g)
        self.guestsRegistered = []
        self.saveState()


    def moveToTVON(self):
        dprint("On TV ON ...")
        self.tv = True
        self.checkEventGen(True)
        self.clearViewership()
        self.validKeys = self.KeyToNum.keys()
        self.display()


    def clearUserPresence(self):
        if self.absent:
            self.absent = not self.absent
        self.guestsRegistered = []


    def moveToInstallationMode(self):
        if not self.is_bm3:
            self.close()
        dprint("In installation mode ...")
        self.in_installation_mode = True
        # For old states since 20s buffer
        self.checkEventGen(True)
        self.clearViewership()
        # For immediate ack
        self.checkEventGen(True)


    def moveOutInstallationMode(self):
        dprint("Moving out of installation mode ...")
        if not self.is_bm3:
            timer=60
            dprint(f"Waiting for {timer}s ...")
            time.sleep(timer)
            if self.checkInstallationMode():
                return
            else:
                if not self.connect():
                    return
        self.in_installation_mode = False
        self.clearViewership()
        self.clearUserPresence()


    def onTVOFF(self):
        dprint("On TV OFF ..")
        self.tv = False
        self.checkEventGen(True)
        self.clearViewership()
        self.validKeys = ["INFO", "ABS", "INCB", "DECB", "CANCEL"]
        self.display()


    def onNewAud(self, current_aud):
        if not self.tv:
            self.checkEventGen(True)
            self.clearGuestRegistration()
            self.cleared_aud = current_aud
            self.saveState()


    def guest_reg(self, guest: Guest):
        dprint(f"Check guest {guest.position} registered?")
        for g in self.guestsRegistered:
            if guest.position == g.position:
                return g
        return False


    def sendEvent(self, body):
        push_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        push_socket.connect(socket_address)
        push_socket.sendall(body)
        push_socket.close()


    def pushEvent(self, toBeRegisteredGuest=None, deReg=None):
        if toBeRegisteredGuest:
            # Registeration
            guest_id = int(toBeRegisteredGuest.position)-1
            guest_age = int(toBeRegisteredGuest.identity[1:])
            body = msgpack.packb(EVENT_VERSION)+msgpack.packb(EVENT_TYPE_GUEST_REG)+\
                msgpack.packb({"Guest_id": guest_id, "Registering": True, \
                               "Guest_age": guest_age, \
                               "Guest_male":toBeRegisteredGuest.identity[0]=="M"})
            dprint("Guest reg event body: ")
            dprint({"Guest_id": guest_id, "Registering": True, \
                           "Guest_age": guest_age, \
                           "Guest_male":toBeRegisteredGuest.identity[0]=="M"})
        elif deReg:
            # Guest De-Reg
            guest_id = int(deReg.position)-1
            guest_age = int(deReg.identity[1:])
            body = msgpack.packb(EVENT_VERSION)+msgpack.packb(EVENT_TYPE_GUEST_REG)+\
                msgpack.packb({"Guest_id": guest_id, "Registering": False, \
                            "Guest_age": guest_age, \
                            "Guest_male":deReg.identity[0]=="M"})
            dprint("Guest de-reg event body: ")
            dprint({"Guest_id": guest_id, "Registering": False, \
                        "Guest_age": guest_age, \
                        "Guest_male":deReg.identity[0]=="M"})
        else:
            # Declaration
            if self.lastCommState.viewersDeclared != self.viewersDeclared:
                member_keys = [False]*12
                guest_keys = [False]*5
                for m in self.viewersDeclared:
                    if len(m) == 1:
                        member_keys[ord(m)-65] = True
                    elif len(m) == 2:
                        guest_keys[int(m[1:])-1] = True
                body = msgpack.packb(EVENT_VERSION)+msgpack.packb(EVENT_TYPE_MEM_GUEST_DECL)+\
                    msgpack.packb({"Member_Keys": member_keys, "Guests": guest_keys, "Confidence": 100})
                dprint("Mem declaration event body: ")
                dprint({"Member_Keys": member_keys, "Guests": guest_keys, "Confidence": 100})
                self.sendEvent(body)
                self.lastCommState.viewersDeclared = copy.deepcopy(self.viewersDeclared)
            if self.lastCommState.absent != self.absent:
                body = msgpack.packb(EVENT_VERSION)+msgpack.packb(EVENT_TYPE_REMOTE_ACTIVITY)+\
                    msgpack.packb({"Lock": False, "ORR": False, "Absent_Key_Press": self.absent, "Drop": False})
                dprint("Remote state event body: ")
                dprint({"Lock": False, "ORR": False, "Absent_Key_Press": self.absent, "Drop": False})
                self.sendEvent(body)
                self.lastCommState.absent = self.absent
            return
        self.sendEvent(body)


    def checkEventGen(self, force: bool=False):
        if (self.stateChangedAt and datetime.datetime.now() - self.stateChangedAt > datetime.timedelta(seconds=20)) or force:
            self.saveState()
            self.pushEvent()


    def checkInstallationMode(self):
        return Path(INSTALLATION_MODE_SENTINEL).exists()


    def dprintStates(self, where: str):
        dprint(f"""

    {where}
        Cleared audience session      : {self.cleared_aud},
        Declared viewers              : {self.viewersDeclared},
        Registered members            : {self.viewersRegistered},
        Registered guests             : {self.guestsRegistered},
        Absence                       : {self.absent},
        TV Status                     : {self.tv},
        Valid keys                    : {self.validKeys},
        guestFlowKeys                 : {self.guestFlowKeys},
        Guest being registered        : {self.toBeRegisteredGuest},
        Guest registration started at : {self.grKeyPressTime},
        Displayed information at      : {self.displayOnTime},
        State Changed at              : {self.stateChangedAt},
        Info Refreshed at             : {self.refreshed_info_at},
        Last known key press          : {self.last_known_key_press},
        In Installation mode          : {self.in_installation_mode},
        Is remote associated          : {self.remote_paired},

        Last comm states              : Declared Viewers: {self.lastCommState.viewersDeclared}, Absent: {self.lastCommState.absent}

        """)


    def is_remote_associated(self):
        if self.in_installation_mode:
            if subprocess.getoutput(f"cat {INSTALLATION_MODE_SENTINEL}") != "with-display-remote":
                return False
        elif int(subprocess.getoutput("get_config REMOTE_ID")) == 0 or int(subprocess.getoutput("get_config REMOTE_ID")) != int(subprocess.getoutput("meter_id")) :
            return False
        return True


    def getTvStatus(self):
        if which("derived_tv_status") is not None:
            tv_status = subprocess.getoutput('derived_tv_status')
        else:
            tv_status = subprocess.getoutput('tv_status')
        return bool(int(tv_status))


class DisplayHandler(Remote):

    def __init__(self):
        """
        Initializes a handler for connected display.

        The handler holds the `:ref: State` data structure
        that controls both display and remote. We initialize
        display using `:ref: init()` provided by display module.

        An internal sleep of `10s` is included if `:ref: init()`
        fails, and retried indefinitely.
        """
        super().__init__()
        while True:
            if self.connect():
                break
            time.sleep(5)

    def connect(self) -> bool:
        notified = False
        while True:
            if self.checkInstallationMode() and not self.is_bm3:
                return False
            self.dspi = dsp.init()
            if not self.dspi:
                if not notified:
                    dprint("Vayve LCD Display not detected")
                    notified = True
                time.sleep(10)
            else:
                if (self.dspi.vid, self.dspi.pid) == (0x2047, 0xf003):
                    max_count=150
                    timer=0.1
                    print(f"Waiting for {max_count*timer} sec ...")
                    count=0
                    while count<max_count:
                        if self.checkInstallationMode() and not self.is_bm3:
                            self.close()
                            return False
                        time.sleep(timer)
                        count+=1
                self.displayOnTime = None
                break
            if (self.is_remote_associated() and self.getTvStatus()) and self.viewersRegistered and not self.viewersDeclared:
                self.buzz()
        dprint(f"Clearing display")
        self.dspi.Clear()
        return True

    def close(self):
        dprint("Closing port ...")
        self.dspi.Close()
        self.dspi = None


    def buzz(self):
        if not self.is_remote_associated():
            dprint("No remote associated, Ignoring beep")
            return
        try:
            subprocess.run("buzz 4 &", shell=True)
        except Exception as e:
            print(f"Got exception while execing beep")


    def parseRC5PlusCode(self, rc5pCode):
        '''
        This function extracts the Toggle bit and command from the rc5p response
        sent by remote.

        The IR Code is of the format
        1 1 T A4 A3 A2 A1 A0 C5 C4 C3 C2 C1 C0 1 1

        Not checking address bits as per BARC instructions

        Raises UnknownRC5Command
        '''
        if (rc5pCode & 0xC003) != 0xC003:         # Integrity check on framing bits
            raise InvalidRC5Command(rc5pCode)

        cmd = (rc5pCode >> 2) & 0x003F
        toggle = (rc5pCode >> 13) & 0x0001

        return cmd, toggle


    def detectKeypress(self, d):
        '''
        Implements the protocol to detect keys-pressed

        Every-time a new button is pressed, toggle bit is toggled.
        '''

        rc5pCode = d.ReadRemoteCmd()
        if not rc5pCode:
            return None

        cmd, toggle = self.parseRC5PlusCode(rc5pCode)

        if toggle == self.lastRemoteCmd['toggle'] and cmd == self.lastRemoteCmd['cmd']:
            return None

        self.lastRemoteCmd['toggle'] = toggle
        self.lastRemoteCmd['cmd'] = cmd

        if cmd in self.NumToKey:
            return self.NumToKey[cmd]
        else:
            return None


    def display(self, info=False, autorefresh=False):
        """
        This func prepares the info needed to be
        displayed based on the `:ref: State` data structure.
        """
        if info:
            top_row = ["WMK:"+str(int(self.wm_status))+"  "+"GSM:"+str(int(self.gsm_status))]
            bottom_row = ["L:"+str(int(self.uploader_status))+"  "]
            if self.getTvStatus():
                bottom_row.append("o")
            else:
                bottom_row.append("f")
        elif self.grKeyPressTime is None:
            top_row = []
            bottom_row = []
            for i in range(65, 77):
                c = chr(i)
                if c in self.viewersRegistered and c not in self.viewersDeclared:
                    c = "_"
                elif c not in self.viewersRegistered:
                    c = "."
                top_row.append(c)
            for i in range(1, 6):
                c = str(i)
                if self.guest_reg(Guest(c)) and "G"+c not in self.viewersDeclared:
                    c = "_"
                elif not self.guest_reg(Guest(c)):
                    c = "."
                bottom_row.append(c)
            bottom_row.append(str(int(self.absent)))
        else:
            if self.guestFlowKeys == self.guestRegState2:
                top_row = ["REG GUEST   "]
                bottom_row = [str(i) if self.guest_reg(Guest(i)) else "*" for i in range(1, 6)]
            elif self.guestFlowKeys == self.guestRegState3:
                group = self.toBeRegisteredGuest.identity
                if group is None:
                    group = "  "
                top_row = [f"A: {self.AgeGroup[group[1:]]}"+f"   {group[0]}"]
                bottom_row = [str(i) if str(i) == self.toBeRegisteredGuest.position else " " for i in range(1, 6)]
            bottom_row.append(";")
        self.dspi.SetBrightness(self.brightnessLevel)
        self.dspi.Send("".join(top_row), "".join(bottom_row))
        if not autorefresh:
            self.displayOnTime = datetime.datetime.now()
        if not info:
            # To disable the refreshInfo routine.
            if self.last_known_key_press == "INFO":
                self.last_known_key_press = None


    def displayTimeout(self, force=False):
        """
        Resets display timer based on `DISPLAY_TIMEOUT`
        """
        if force or (self.displayOnTime and datetime.datetime.now() - self.displayOnTime > datetime.timedelta(seconds=DISPLAY_TIMEOUT)):
            if not self.tv:
                self.dspi.Clear()
            self.displayOnTime = None
            self.last_known_key_press = None


    def clearGRFlow(self):
        """
        Clears guest registration flow by resetting the
        guest-reg flow related vars in `:ref: State`
        """
        self.toBeRegisteredGuest = None
        self.grKeyPressTime = None
        self.guestFlowKeys = None
        self.display()


    def handleRegistration(self, key: str)->bool:
        """
        Implements the state transition logic for
        guest registration based on the key press.
        """
        if key in self.guestRegState2:
            self.toBeRegisteredGuest=self.guest_reg(Guest(key[1:]))
            if not self.toBeRegisteredGuest:
                self.toBeRegisteredGuest=Guest(key[1:])
            self.guestFlowKeys = self.guestRegState3
            done = False
        elif key in self.guestRegState3:
            if key != "OK":
                self.toBeRegisteredGuest.identity = key
                done = False
            else:
                if not self.guest_reg(self.toBeRegisteredGuest):
                    self.guestsRegistered.append(self.toBeRegisteredGuest)
                if "G"+self.toBeRegisteredGuest.position not in self.viewersDeclared:
                    self.viewersDeclared.append("G"+self.toBeRegisteredGuest.position)
                    self.viewersDeclared.sort()
                self.pushEvent(self.toBeRegisteredGuest)
                # Since the `saveState` will clear the timer
                self.pushEvent()
                self.saveState()
                self.clearGRFlow()
                done = True
        self.display()
        self.grKeyPressTime = datetime.datetime.now()
        return done


    def guestKeyPress(self):
        """
        Guest-reg key press routine.
        """
        count=0
        while True:

            if datetime.datetime.now() - self.grKeyPressTime > datetime.timedelta(seconds=GREG_KP_TIMEOUT):
                self.clearGRFlow()
                return

            if not (self.is_remote_associated() and self.getTvStatus()):
                self.clearGRFlow()
                self.onTVOFF()
                return

            if hex(self.dspi.pid) == "0xf003":
                count+=1
                if count%5==0:
                    if self.guestFlowKeys == self.guestRegState2:
                        self.dspi.lightChar("G")
                        self.dspi.clearChar("G")
                    elif self.guestFlowKeys == self.guestRegState3:
                        self.dspi.lightChar("A")
                        self.dspi.clearChar("A")

            try:
                key = self.detectKeypress(self.dspi)
            except InvalidRC5Command as e:
                self.dspi.Flush()
                print(f"Unknown Code received from remote: {e}")
                continue

            if not key:
                time.sleep(0.1)
                continue

            if key == "CANCEL":
                self.clearGRFlow()
                return

            self.dprintStates("In guest flow ..")
            print(f"New Key press received for key: {key}")
            if key not in self.guestFlowKeys:
                continue

            if self.handleRegistration(key):
                self.clearGRFlow()
                return

            time.sleep(0.1)


    def guestRegistration(self, key: str):
        """
        Func that initiates guest reg when `GUEST`
        key press is detected
        """
        self.guestFlowKeys = self.guestRegState2
        self.grKeyPressTime = datetime.datetime.now()
        self.dspi.Clear()
        self.display()
        self.guestKeyPress()


    def handleDeclaration(self, key: str):
        """
        Declaration keys are considered valid only
        if they are registered

        Effect: `stateChangedAt` is only considered in these
        cases
        """
        if key in self.guestRegState2 and not self.guest_reg(Guest(key[1:])):
            self.grKeyPressTime = datetime.datetime.now()
            self.dspi.Clear()
            self.handleRegistration(key)
            self.guestKeyPress()
            return
        if key in self.viewers and (key in self.viewersRegistered or self.guest_reg(Guest(key[1:]))):
            if key in self.viewersDeclared:
                self.viewersDeclared.remove(key)
            else:
                self.viewersDeclared.append(key)
            self.viewersDeclared.sort()
            self.display()
            if not self.stateChangedAt:
                self.stateChangedAt = datetime.datetime.now()


    def handleInfo(self, autorefresh=False):
        try:
            scores = subprocess.getoutput("cat /run/wm_scores")
            self.wm_status = sum(list(map(int, scores.split(" ")))) >= 2
        except Exception as e:
            dprint(f"Got exception while querying for wm scores")

        try:
            self.gsm_status = any(s in subprocess.getoutput('cat /run/SIM_"$(cat /run/current-sim)"_status') for s in ["Spotty", "OK"])
        except Exception as e:
            dprint(f"Got exception while querying for sim 1 status")

        try:
            self.uploader_status = Path("/run/uploader_connected").is_file()
        except Exception as e:
            dprint(f"Got exception while querying for uploader status")

        self.display(info=True, autorefresh=autorefresh)
        self.refreshed_info_at = datetime.datetime.now()


    def handleKey(self, key):
        """
        Remote key press handler
        """
        if key in self.viewers:
            self.handleDeclaration(key)
        elif key in self.guestRegState1:
            self.guestRegistration(key)
        elif key in ["INFO"]:
            self.handleInfo()
        elif key in ["ABS"]:
            if not self.stateChangedAt:
                self.stateChangedAt = datetime.datetime.now()
            self.absent = not self.absent
            self.display()
        elif key in ["OK"]:
            self.checkEventGen(True)
            self.display()
        elif key == "INCB":
            self.brightnessLevel+=BRIGHTNESS_LEVEL_STEP
            self.brightnessLevel = min(self.brightnessLevel, MAX_ALLOWED_BRIGHTNESS)
            self.dspi.SetBrightness(self.brightnessLevel)
        elif key == "DECB":
            self.brightnessLevel-=BRIGHTNESS_LEVEL_STEP
            self.brightnessLevel = max(self.brightnessLevel, MIN_ALLOWED_BRIGHTNESS)
            self.dspi.SetBrightness(self.brightnessLevel)
        elif key == "CANCEL":
            if self.displayOnTime is not None:
                self.displayTimeout(force=True)
        self.last_known_key_press = key


    def refreshInfo(self):
        """
        Refreshes the display if the last key pressed is `INFO`.
        """
        if self.last_known_key_press == "INFO" and self.displayOnTime and (datetime.datetime.now() - self.refreshed_info_at > datetime.timedelta(seconds=INFO_REFRESH_TIMEOUT)):
            print(f"Refreshing INFO")
            self.handleInfo(autorefresh=True)
            self.refreshed_info_at = datetime.datetime.now()


    def run(self):
        """
        Remote key press detection routine
        """
        if not self.tv:
            self.onTVOFF()

        def inNewAud() -> bool:
            return (
                self.cleared_aud is None or
                (
                    datetime.datetime.now().strftime(f"%Y-%m-%d {AUDIENCE_SESSION_CLOSE_TIME}") != self.cleared_aud and
                    datetime.datetime.now().strftime(f"%Y-%m-%d %H:%M:%S") > datetime.datetime.now().strftime(f"%Y-%m-%d {AUDIENCE_SESSION_CLOSE_TIME}")
                )
            )


        if inNewAud():
            self.onNewAud(datetime.datetime.now().strftime(f"%Y-%m-%d {AUDIENCE_SESSION_CLOSE_TIME}"))
        self.dprintStates("main")
        self.display()
        while True:
            self.checkEventGen()

            tv_status = self.getTvStatus()
            remote_paired_status = self.is_remote_associated()

            if self.in_installation_mode and not self.checkInstallationMode():
                self.moveOutInstallationMode()
                self.viewersRegistered = self.readMemberConfig()
                if self.remote_paired:
                    self.display()
            elif not self.in_installation_mode and self.checkInstallationMode():
                self.moveToInstallationMode()
                if self.is_bm3:
                    self.viewersRegistered = self.readMemberConfig()
                    if self.remote_paired:
                        self.display()

            if self.in_installation_mode and not self.is_bm3:
                time.sleep(5)
                continue

            self.displayTimeout()
            self.refreshInfo()

            if self.tv and not (remote_paired_status and tv_status):
                self.onTVOFF()
            if not self.tv and (remote_paired_status and tv_status):
                self.moveToTVON()

            if self.remote_paired and not remote_paired_status:
                self.remote_paired = False
                self.clearUserPresence()
            if not self.remote_paired and remote_paired_status:
                self.remote_paired = True

            if inNewAud():
                self.onNewAud(datetime.datetime.now().strftime(f"%Y-%m-%d {AUDIENCE_SESSION_CLOSE_TIME}"))

            if (self.remote_paired and self.tv) and self.viewersRegistered and not self.viewersDeclared:
                # This will give sometime for user-input
                if not self.displayOnTime:
                    self.display()
                    self.buzz()

            try:
                key = self.detectKeypress(self.dspi)
            except InvalidRC5Command as e:
                self.dspi.Flush()
                print(f"Unknown Code received from remote: {e}")
                continue

            if not key:
                time.sleep(0.1)
                continue

            print(f"New Key press received for key: {key}")

            if key in self.validKeys:
                self.handleKey(key)

            time.sleep(0.1)


def main():
    global AUDIENCE_SESSION_CLOSE_TIME, VERBOSE
    AUDIENCE_SESSION_CLOSE_TIME =  os.environ["AUDIENCE_SESSION_CLOSE_TIME"]
    if AUDIENCE_SESSION_CLOSE_TIME is None:
        raise RuntimeError("Couldn't find AUDIENCE_SESSION_CLOSE_TIME env")
    # Assuming that the value is always in UTC.
    AUDIENCE_SESSION_CLOSE_TIME = (datetime.datetime.strptime(AUDIENCE_SESSION_CLOSE_TIME, "%H:%M:%S") + datetime.timedelta(hours=5, minutes=30)).strftime("%H:%M:%S")
    VERBOSE = bool(int(os.environ["VERBOSE"]))
    dsh = DisplayHandler()
    dsh.run()


if __name__ == "__main__":
    main()
