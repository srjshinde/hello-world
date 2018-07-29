import time
from firebase import firebase
import RPi.GPIO  as g
import serial

g.setmode(g.BOARD)
g.setup(38, g.IN, pull_up_down=g.PUD_DOWN)
g.setup(40,g.OUT)

sent= False
#balance1= 200
#reading1= 40
#prev_balance=200

port= serial.Serial("/dev/ttyUSB0",9600,timeout= 1)

firebase=firebase.FirebaseApplication('https://prepaidm123.firebaseio.com', None)

balance1 = firebase.get('/Master/Balance',None)
reading1 = balance1/5					#firebase.get('/reading1',None)

def gsm_init():
	port.write('AT'+'\r')
	rcv = port.readline()
	print rcv
	time.sleep(1)
 
	port.write('ATE0'+'\r')      # Disable the Echo
	rcv = port.readline()
	print rcv
	time.sleep(1)
 
	port.write('AT+CMGF=1'+'\r')  # Select Message format as Text mode 
	rcv = port.readline()
	print rcv
	time.sleep(1)
 
	port.write('AT+CNMI=2,1,0,0,0'+'\r')   # New SMS Message Indications
	rcv = port.readline()
	print rcv
	time.sleep(1)

gsm_init()

def send_sms():

	global sent
	port.write('AT+CMGS= "9503436450"'+'\r')
	rcv = port.readline()
	print rcv
	time.sleep(1)
	#print "hell"
	port.write('Hello User'+'\r')  # Message
	rcv = port.readline()
	print rcv

	port.write("\x1A") # Enable to send SMS
	sent= True
	print 'SMS sent...'


def firebase_update():

    global balance1 ,reading1
    firebase.put('','/Master/Balance',balance1)
    firebase.put('','/Master/MeterReading',reading1)

def read_pulse():

    global balance1, reading1, sent
    if g.input(38):
        
	balance1 = firebase.get('/Master/Balance',None)
	
        balance1 = balance1 - 5
        reading1 = reading1 - 1
        
	
        firebase_update()
        
        g.output(40,True)
        time.sleep(0.3)
        g.output(40,False)

	if balance1 > 50:
		sent= False
        
        while g.input(38):
            reading1=reading1
        
        
while True:
    try:

        read_pulse()

	if sent== False and balance1 < 50:
		send_sms()
	
        
    except IOError:
        
        print "exited successfully"
