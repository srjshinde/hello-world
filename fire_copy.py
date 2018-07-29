import time
from firebase import firebase
import RPi.GPIO  as g
import serial

g.setmode(g.BOARD)
g.setup(38,g.IN)
g.setup(40,g.OUT)

balance1= 200
reading1= 40
balance2= 200
reading2= 40
balance3= 200
reading3= 40
sent= False
#prev_balance=200

firebase=firebase.FirebaseApplication('https://nodemcu-first.firebaseio.com', None)

port= serial.Serial("/dev/ttyUSB0",9600,timeout=1)          #use try except here

def gsm_init()
    port.write('AT'+'\r')
    rcv = port.read(10)
    print rcv
    time.sleep(0.4)
 
    port.write('ATE0'+'\r')      # Disable the Echo
    rcv = port.read(10)
    print rcv
    time.sleep(0.4)
 
    port.write('AT+CMGF=1'+'\r')  # Select Message format as Text mode 
    rcv = port.read(10)
    print rcv
    time.sleep(0.4)
 
    port.write('AT+CNMI=2,1,0,0,0'+'\r')   # New SMS Message Indications
    rcv = port.read(10)
    print rcv
    time.sleep(0.4)
    
gsm_init()

balance1 = firebase.get('/balance1',None)
reading1 = firebase.get('/reading1',None)

def firebse_update():
    global balance1, reading1               #balance3, reading1, reading2, reading3
    firebase.put('','balance1',balance1)
    firebase.put('','reading1',reading1)
    firebase.put('','balance2',balance1+100)
    firebase.put('','reading2',reading1+20)
    firebase.put('','balance3',balance1+200)
    firebase.put('','reading3',reading1+40)

def port_read():
    global gsm
    gsm= port.readline()

    if gsm.find("*#*#") != -1:
        start= gsm.find("*#*#")
        end= gsm.find("#*#*")
        balance1= int(gsm[start]:gsm[end])
        firebase_update()
        
def low_bal_sms():
    port.write('AT+CMGS="9665916383"'+'\r')
    rcv = port.read(10)
    print rcv
    time.sleep(1)
 
    port.write('Low balance alert:\nDear customer,\nyour a/c balance is: %s\nplease recharge your a/c soon.'+'\r' % (balance1))  # Message
    rcv = port.read(10)
    print rcv
 
    port.write("\x1A") # Enable to send SMS
    sent= True

def read_pulse():
    if g.input(38):
        
        balance1 = balance1 - 5
        reading1 = reading1 - 1
        
        firebase_update()
        
        g.output(40,True)
        time.sleep(0.3)
        g.output(40,False)
        
        while g.input(38):
            reading1=reading1
        
        
while True:
    try:
        
        read_pulse()

        if sent==False and balance1 < 20:
            low_bal_sms()

        port_read()
        
        if balance1 > 20:
            sent= False
        
    except IOError:
        
        print "exited successfully"
