#!/usr/bin/python

# keg control system
# by mike wakerly; mike@wakerly.com

import os, time
import logging
from onewirenet import *
from ibutton import *
from mtxorb import *
from lcdui import *
from output import *
from ConfigParser import ConfigParser
import thread, threading
import signal
import readline
import traceback

from KegRemoteServer import KegRemoteServer
from toc import BotManager
from SQLStores import *
from SQLHandler import *
from FlowController import *
from TempMonitor import *

# edit this line to point to your config file; that's all you have to do!
config = 'keg.cfg'

# ---------------------------------------------------------------------------- #
# Helper functions -- available to all classes
# ---------------------------------------------------------------------------- #
def instantBAC(user,keg,drink_ticks):
   # calculate weight in metric KGs
   if user.weight <= 0:
      return 0.0

   kg_weight = user.weight/2.2046
   ounces = keg.getDrinkOunces(drink_ticks)

   # gender based water-weight
   if user.gender == 'male':
      waterp = 0.58
   else:
      waterp = 0.49

   # find total body water (in milliliters)
   bodywater = kg_weight * waterp * 1000.0

   # weight in grams of 1 oz alcohol
   alcweight = 29.57*0.79;

   # rate of alcohol per subject's total body water
   alc_per_body_ml = alcweight/bodywater

   # find alcohol concentration in blood (80.6% water)
   alc_per_blood_ml = alc_per_body_ml * 0.806

   # switch to "grams percent"
   grams_pct = alc_per_blood_ml * 100.0
   #print "grams pct: %s" % grams_pct

   # determine how much we've really consumed
   alc_consumed = keg.getDrinkOunces(drink_ticks) * (keg.alccontent/100.0)
   instant_bac = alc_consumed * grams_pct

   return instant_bac

def decomposeBAC(bac,seconds_ago,rate=0.02):
   return max(0.0,bac - (rate * (seconds_ago/3600.0)))

def toF(self,t):
   return ((9.0/5.0)*t) + 32

# ---------------------------------------------------------------------------- #
# Main classes
# ---------------------------------------------------------------------------- #

class KegBot:
   """ the thinking kegerator! """
   def __init__(self,config):

      # this init function is now split in to two sections, to support online
      # reloading of compiled component code.

      self.QUIT = threading.Event() # event to set when we want everything to quit
      self.setsigs() # set up handlers for control-c, kill signals

      self.config = ConfigParser()

      self.verbose = 0
      self.last_temp = -100.0
      self.last_temp_time = 0
      self.ibs = []
      self._allibs = []
      self.ibs_seen = {} # store time when IB was last seen

      # a list of buttons (probably just zero or one) that have been connected
      # for too long. if in this list, the mainEventLoop will wait for the
      # button to 'go away' for awhile until it will recognize it again. among
      # other things, this keeps a normally-closed solenoid valve from burning
      # out
      self.timed_out = []

      # used for auditing between pours. see comments inline.
      self.last_flow_ticks = None

      # ready to perform second stage of initialization
      self._setup()

      # start everything up
      self.mainEventLoop()

   def _setup(self):

      # read the config
      self.config.read(config)

      # load the db info, because we will use it often enough
      self.dbhost = self.config.get('DB','host')
      self.dbuser = self.config.get('DB','user')
      self.dbdb = self.config.get('DB','db')
      self.dbpassword = self.config.get('DB','password')
      self.logtable = self.config.get('Logging','logtable')

      # set up logging, using the python 2.3 logging module
      self.main_logger = self.makeLogger('main',logging.INFO)

      # set up the drink, user, and key stores. these classes provide read,
      # write, and search access to information that the keg needs to know
      # about.

      # rather than retyping this stuff on each init line, just add this tuple
      # and the table name to form the init tuple
      db_tuple = (self.dbhost,self.dbuser,self.dbpassword,self.dbdb)

      self.drink_store   = DrinkStore(  db_tuple, self.config.get('DB','drink_table') )
      self.user_store    = UserStore(   db_tuple, self.config.get('DB','user_table') )
      self.key_store     = KeyStore(    db_tuple, self.config.get('DB','key_table') )
      self.policy_store  = PolicyStore( db_tuple, self.config.get('DB','policy_table') )
      self.grant_store   = GrantStore(  db_tuple, self.config.get('DB','grant_table') , self.policy_store)
      self.keg_store     = KegStore(    db_tuple, self.config.get('DB','keg_table') )
      self.thermo_store  = ThermoStore( db_tuple, self.config.get('DB','thermo_table') )

      # set up the import stuff: the ibutton onewire network, and the LCD UI
      self.netlock = threading.Lock()
      dev = self.config.get('Devices','onewire')
      try:
         self.ownet = onewirenet(dev)
         self.info('main','new onewire net at device %s' % dev)
      except:
         self.error('main','not connected to onewirenet')

      # load the LCD-UI stuff
      if self.config.getboolean('UI','use_lcd'):
         dev = self.config.get('Devices','lcd')
         self.info('main','new LCD at device %s' % dev)
         self.lcd = Display(dev,model=self.config.get('UI','lcd_model'))
         self.ui = lcdui(self.lcd)
      else:
         self.lcd = Display('/dev/null')
         self.ui = lcdui(self.lcd)

      # init flow meter
      dev = self.config.get('Devices','flow')
      self.info('main','new flow controller at device %s' % dev)
      self.fc = FlowController(dev)
      self.last_fridge_time = 0 # time since fridge event (relay trigger)

      # set up the default 'screen'. for now, it is just a boring standard
      self.main_plate = plate_kegbot_main(self.ui)
      self.ui.setCurrentPlate(self.main_plate)
      self.ui.start()
      self.ui.activity()

      # set up the remote call server, for anything that wants to monitor the keg
      #host = self.config.get('Remote','host')
      #port = self.config.get('Remote','port')
      #self.cmdserver = KegRemoteServer(self,host,port)
      #self.cmdserver.start()

      self.io = KegShell(self)
      self.io.start()

      # start the refresh loop, which will keep self.ibs populated with the current onewirenetwork.
      thread.start_new_thread(self.ibRefreshLoop,())
      time.sleep(1.0) # sleep to wait for ibrefreshloop - XXX

      # start the temperature monitor
      if self.config.getboolean('Thermo','use_thermo'):
         self.tempsensor = TempSensor(self.config.get('Devices','thermo'))
         self.tempmon = TempMonitor(self,self.tempsensor,self.QUIT)
         self.tempmon.start()

      # start the aim bot
      if self.config.getboolean('AIM','use_aim'):
         from KegAIMBot import KegAIMBot
         sn = self.config.get('AIM','screenname')
         pw = self.config.get('AIM','password')
         self.aimbot = KegAIMBot(sn,pw,self)
         self.bm = BotManager()
         self.bm.addBot(self.aimbot,"aimbot",go=1)

   def setsigs(self):
      signal.signal(signal.SIGHUP, self.handler)
      signal.signal(signal.SIGINT, self.handler)
      signal.signal(signal.SIGQUIT,self.handler)
      signal.signal(signal.SIGTERM, self.handler)

   def handler(self,signum,frame):
      self.quit()

   def quit(self):
      self.info('main','attempting to quit')
      self.QUIT.set()
      self.ui.stop()
      if self.config.getboolean('AIM','use_aim'):
         self.aimbot.saveSessions()
      #self.cmdserver.stop()

   def makeLogger(self,compname,level=logging.INFO):
      """ set up a logging logger, given the component name """
      ret = logging.getLogger(compname)
      ret.setLevel(level)

      # add sql handler
      if self.config.getboolean('Logging','use_sql'):
         try:
            hdlr = SQLHandler(self.dbhost,self.dbuser,self.dbdb,self.logtable,self.dbpassword)
            formatter = SQLVerboseFormatter()
            hdlr.setFormatter(formatter)
            ret.addHandler(hdlr)
         except:
            ret.warning("Could not start SQL Handler")

      # add a file-output handler
      if self.config.getboolean('Logging','use_logfile'):
         hdlr = logging.FileHandler(self.config.get('Logging','logfile'))
         formatter = logging.Formatter(self.config.get('Logging','logformat',raw=1))
         hdlr.setFormatter(formatter)
         ret.addHandler(hdlr)

      # add tty handler
      if self.config.getboolean('Logging','use_stream'):
         hdlr = logging.StreamHandler(sys.stdout)
         formatter = logging.Formatter(self.config.get('Logging','logformat',raw=1))
         hdlr.setFormatter(formatter)
         ret.addHandler(hdlr)

      return ret

   def enableFreezer(self):
      curr = self.tempmon.sensor.getTemp(1) # XXX - sensor index is hardcoded! add to .config
      max = self.config.getfloat('Thermo','temp_max_high')

      # refuse to enable the fridge if we just disabled it. (we don't do this
      # in the disableFreezer routine, because we should always be allowed to
      # disable it.)
      min_diff = self.config.getint('Timing','freezer_event_min')
      diff = time.time() - self.last_fridge_time

      if self.fc.UNKNOWN or self.fc.fridgeStatus() == False: 
         if diff < min_diff:
            self.warning('tempmon','fridge event requested less than %i seconds after last, ignored (%i)' % (min_diff,diff))
            return
         self.last_fridge_time = time.time()
         self.fc.UNKNOWN = False
         self.info('tempmon','activated freezer curr=%s max=%s'%(curr[0],max))
         self.main_plate.setFreezer('on ')
         self.fc.enableFridge()

   def disableFreezer(self):
      curr = self.tempmon.sensor.getTemp(1)
      min = self.config.getfloat('Thermo','temp_max_low')

      # note: no check here for recent fridge event, because we will always
      # allow the fridge to be disabled.
      self.last_fridge_time = time.time()

      if self.fc.UNKNOWN or self.fc.fridgeStatus() == True:
         self.fc.UNKNOWN = False
         self.info('tempmon','disabled freezer curr=%s min=%s'%(curr[0],min))
         self.main_plate.setFreezer('off')
         self.fc.disableFridge()

   def ibRefreshLoop(self):
      """
      Periodically update self.ibs with the current ibutton list.

      Because there are at least two threads (temperature monitor, main event
      loop) that require fresh status of the onewirenetwork, it is useful to
      simply refresh them constantly.

      Note that the config file may specify IB IDs to ignore (such as the
      serial controller ID or other persistent IBs). These IDs will be sored in
      _allibs but not self.ibs, and that is the only difference.
      """
      timeout = self.config.getfloat('Timing','ib_refresh_timeout')
      ignore_ids = self.config.get('Users','ignoreids').split(" ")

      while not self.QUIT.isSet():
         self.netlock.acquire()
         self._allibs = self.ownet.refresh()
         self.netlock.release()
         self.ibs = [ib for ib in self._allibs if ib.read_id() not in ignore_ids]
         now = time.time()
         for ib in self.ibs:
            self.ibs_seen[ib.read_id()] = now
         time.sleep(timeout)

      self.info('ibRefreshLoop','quit!')

   def lastSeen(self,ibname):
      if self.ibs_seen.has_key(ibname):
         return self.ibs_seen[ibname]
      else:
         return 0

   def mainEventLoop(self):
      while not self.QUIT.isSet():
         time.sleep(0.5)
         uib = None

         # remove any tokens from the 'idle' list. assume the config value
         # ib_idle_min_disconnected is set to 5 seconds. require each kicked
         # button to have been seen 5 or more seconds ago. (eg, not seen in
         # last 5 seconds).
         cutoff = time.time() - self.config.getint('Timing','ib_idle_min_disconnected')
         self.timed_out = [x for x in self.timed_out if self.lastSeen(x) > cutoff]

         # now get down to business.
         for ib in self.ibs:
            if self.key_store.knownKey(ib.read_id()) and ib.read_id() not in self.timed_out:
               time_since_seen = time.time() - self.lastSeen(ib.read_id())
               ceiling = self.config.getfloat('Timing','ib_missing_ceiling')
               if time_since_seen < ceiling:
                  self.info('flow','found an authorized ibutton: %s' % ib.read_id())

                  # note: break call at the end of this block ensures that,
                  # after a flow is handled, this mainEventLoop re-starts with
                  # fresh data (eg self.ibs)
                  self.handleFlow(ib)
                  break

   def handleFlow(self,uib):
      self.info('flow','starting flow handling')
      self.ui.activity()
      current_keg = self.keg_store.getCurrentKeg()

      current_user = self.getUser(uib)
      if not current_user:
         self.error('flow','no valid user for this key; how did we get here?')
         return

      grants = self.grant_store.getGrants(current_user)
      ordered = self.grant_store.orderGrants(grants)

      try:
         current_grant = ordered.pop(0)
      except IndexError:
         self.info('flow','no valid grants found; not starting flow')
         self.timeoutToken(uib.read_id())
         return

      self.info('flow',"current grant: %s" % (current_grant.policy.descr))

      # sequence of steps that should take place:
      # - prepare counter
      self.initFlowCounter()

      # - record flow counter
      initial_flow_ticks = self.fc.readTicks()
      last_reading = initial_flow_ticks
      self.info('flow','current flow ticks: %s' % initial_flow_ticks)

      # - turn on UI
      user_screen = self.makeUserScreen(current_user)
      self.ui.setCurrentPlate(user_screen,replace=1)

      # - turn on flow
      self.fc.openValve()
      drink_start = time.time()

      # - wait for ibutton release OR inaction timeout
      self.info('flow','starting flow for user %s' % current_user.getName())
      STOP_FLOW = 0

      # - start the timout counter
      idle_timeout = self.config.getfloat('Timing','ib_idle_timeout')
      t = threading.Timer(idle_timeout,self.timeoutToken,(uib.read_id(),))
      t.start()

      ounces = 0.0
      ceiling = self.config.getfloat('Timing','ib_missing_ceiling')

      # set up the record for logging
      rec = DrinkRecord(self.drink_store,current_user.id,current_keg)

      #
      # flow maintenance loop
      #
      last_flow_time = 0
      ticks,grant_ticks = 0,0
      old_grant = None

      while 1:
         # if we've expired the grant, log it
         if current_grant.isExpired(current_keg.getDrinkOunces(grant_ticks)):
            rec.addFragment(current_grant,grant_ticks)
            grant_ticks = 0
            try:
               current_grant = ordered.pop(0)
               while current_grant.isExpired():
                  current_grant = ordered.pop(0)
            except:
               current_grant = None

         # if no more grants, no more beer
         if not current_grant:
            self.info('flow','no more valid grants; ending flow')
            self.timeoutToken(uib.read_id())
            STOP_FLOW = 1
         else:
            old_grant = current_grant

         # if the token has been gone awhile, end
         time_since_seen = time.time() - self.lastSeen(uib.read_id())
         if time_since_seen > ceiling:
            self.info('flow',red('ib went missing, ending flow (%s,%s)'%(time_since_seen,ceiling)))
            STOP_FLOW = 1

         # check other credentials necessary to keep the beer flowing!
         if self.QUIT.isSet():
            STOP_FLOW = 1

         elif uib.read_id() in self.timed_out:
            STOP_FLOW = 1

         if STOP_FLOW:
            break

         if time.time() - last_flow_time > self.config.getfloat("Flow","polltime"):
            last_flow_time = time.time()

            # tick-incrementing block
            nowticks = self.fc.readTicks()
            delta = nowticks - last_reading
            if delta < 0 or delta > 500: # XXX better estimate
               self.warning('flow','CAUTION: observed tick delta is %i' % delta)
            else:
               ticks += delta
               grant_ticks += delta
            last_reading = max(0,nowticks) # XXX takes 3 readings to normalize assuming 2 errors in a row
            oz = "%s oz    " % round(self.fc.ticksToOunces(ticks),1)

            user_screen.write_dict['progbar'].setProgress(self.fc.ticksToOunces(ticks)/8.0 % 1)
            user_screen.write_dict['ounces'].setData(oz[:6])

      # at this point, the flow maintenance loop has exited. this means
      # we must quickly disable the beer flow and kick the user off the
      # system

      # cancel the idle timeout
      t.cancel()

      # - turn off flow
      self.info('flow','user is gone; flow ending')
      self.fc.closeValve()
      self.ui.setCurrentPlate(self.main_plate,replace=1)

      # - record flow totals; save to user database
      # tick-incrementing block
      nowticks = self.fc.readTicks()
      delta = nowticks - last_reading
      if delta < 0 or delta > 500: # XXX better estimate
         self.warning('flow','CAUTION: observed tick delta is %i' % delta)
      else:
         ticks += delta
         grant_ticks += delta

      rec.addFragment(old_grant,grant_ticks)

      # add the final total to the record
      old_drink = self.drink_store.getLastDrink(current_user.id)
      bac = instantBAC(current_user,current_keg,ticks)
      bac += decomposeBAC(old_drink[0],time.time()-old_drink[1])
      rec.emit(ticks,current_grant,grant_ticks,bac)

      ounces = round(self.fc.ticksToOunces(ticks),1)
      self.main_plate.setLastDrink(current_user.getName(),ounces)
      self.info('flow','drink total: %i ticks, %.2f ounces' % (ticks, ounces))

      # - audit the current flow meter reading
      # this amount, self.last_flow_ticks, is used by initFlowCounter.
      # when the next person pours beer, this amount can be compared to
      # the FlowController's tick reading. if the two readings are off by
      # much, then this may be indicitive of a leak, stolen beer, or
      # another serious problem.
      self.last_flow_ticks = ticks
      self.info('flow','flow ended with %s ticks' % ticks)

      # - back to idle UI

   def timeoutToken(self,id):
      self.info('timeout','timing out id %s' % id)
      self.timed_out.append(id)

   def getUser(self,ib):
      key = self.key_store.getKey(ib.read_id())
      if key:
         return self.user_store.getUser(key.getOwner())
      return None

   def makeUserScreen(self,user):
      scr = plate_std(self.ui)

      namestr = "hello %s" % user.getName()
      while len(namestr) < 16:
         if len(namestr)%2 == 0:
            namestr = namestr + ' '
         else:
            namestr = ' ' + namestr
      namestr = namestr[:16]

      line1 = widget_line_std("*------------------*",row=0,col=0,scroll=0)
      line2 = widget_line_std("| %s |"%namestr,      row=1,col=0,scroll=0)
      progbar = widget_progbar(row = 2, col = 2, prefix ='[', postfix=']', proglen = 9)
      #line3 = widget_line_std("| [              ] |",row=2,col=0,scroll=0)
      line4 = widget_line_std("*------------------*",row=3,col=0,scroll=0)

      pipe1 = widget_line_std("|", row=2,col=0,scroll=0,fat=0)
      pipe2 = widget_line_std("|", row=2,col=19,scroll=0,fat=0)
      ounces = widget_line_std("", row=2,col=12,scroll=0,fat=0)

      scr.updateObject('line1',line1)
      scr.updateObject('line2',line2)
      #scr.updateObject('line3',line3)
      scr.updateObject('progbar',progbar)
      scr.updateObject('pipe1',pipe1)
      scr.updateObject('pipe2',pipe2)
      scr.updateObject('ounces',ounces)
      scr.updateObject('line4',line4)

      return scr

   def debug(self,msg):
      print "[debug] %s" % (msg,)

   def initFlowCounter(self):
      """
      this function is to be called whenever the flow is about to be enabled.

      it may also log any deviation that is noticed.
      """
      if self.last_flow_ticks:
         curr_ticks = self.fc.readTicks()
         if self.last_flow_ticks != curr_ticks:
            self.warning('security','last recorded flow count (%s) does not match currently observed flow count (%s)' % (self.last_flow_ticks,curr_ticks))
      self.fc.clearTicks()

   def log(self,component,message):
      self.main_logger.info("%s: %s" % (component,message))

   def info(self,component,message):
      self.main_logger.info("%s: %s" % (component,message))

   def warning(self,component,message):
      self.main_logger.warning("%s: %s" % (component,message))

   def error(self,component,message):
      self.main_logger.error("%s: %s" % (component,message))

   def critical(self,component,message):
      self.main_logger.critical("%s: %s" % (component,message))

   def addUser(self,username,name = None, init_ib = None, admin = 0, email = None,aim = None):
      uid = self.user_store.addUser(username,email,aim)
      self.key_store.addKey(uid,str(init_ib))

class KegShell(threading.Thread):
   def __init__(self,owner):
      threading.Thread.__init__(self)
      self.owner = owner
      self.commands = ['quit','adduser','showlog','hidelog', 'bot', 'showtemp']

      # setup readline to do fancy tab completion!
      self.completer = Completer()
      self.completer.set_choices(self.commands)
      readline.parse_and_bind("tab: complete")
      readline.set_completer(self.completer.complete)

   def run(self):
      while 1:
         try:
            input = self.prompt()
            tokens = string.split(input,' ')
            cmd = string.lower(tokens[0])
         except:
            raise

         if cmd == 'quit':
            self.owner.quit()
            return

         if cmd == 'showlog':
            self.owner.verbose = 1

         if cmd == 'hidelog':
            self.owner.verbose = 0

         if cmd == 'bot':
            try:
               subcmd = tokens[1]
               if subcmd == 'go':
                  self.owner.bm.botGo("aimbot")
               elif subcmd == 'stop':
                  self.owner.bm.botStop("aimbot")
               elif subcmd == 'say':
                  user  = tokens[2]
                  msg = raw_input('message: ')
                  self.owner.aimbot.do_SEND_IM(user,msg)
            except:
               pass

         if cmd == 'showtemp':
            try:
               temp = self.owner.tempsensor._temps[1]
               print "last temp: %.2i C / %.2i F" % (temp,toF(temp))
            except:
               pass

         if cmd == 'adduser':
            user = self.adduser()
            username,admin,aim,initib = user

            print "got user: %s" % str(username)

            try:
               self.owner.addUser(username,init_ib = initib,admin=admin,aim=aim)
               print "added user successfully"
            except:
               print "failed to create user"
               raise

   def prompt(self):
      try:
         prompt = "[KEGBOT] "
         cmd = raw_input(prompt)
      except:
         cmd = ""
      return cmd

   def adduser(self):
      print "please type the unique username for this user."
      username = raw_input("username: ")
      print "will this user have admin privileges?"
      admin = raw_input("admin [y/N]: ")
      print "please type the user's aim name, if known"
      aim = raw_input("aim name [none]: ")
      print "would you like to associate a particular beerkey with this user?"
      print "here are the buttons i see on the network:"
      count = 0
      for ib in self.owner.ibs:
         print "[%i] %s (%s)" % (count,ib.name,ib.read_id())
         count = count+1
      key = raw_input("key number [none]: ")
      try:
         ib = self.owner.ibs[int(key)]
         key = ib.read_id()
         print "selected %s" % key
      except:
         key = None

      if string.lower(admin)[0] == 'y':
         admin = 1
      else:
         admin = 0

      if aim == "" or aim == "\n":
         aim = None

      if key == "" or key == "\n":
         key = None

      return (username,admin,aim,key)


class Completer:
   def __init__(self):
      self.list = []

   def complete(self, text, state):
      if state == 0:
         self.matches = self.get_matches(text)
      try:
         return self.matches[state]
      except IndexError:
         return None

   def set_choices(self, list):
       self.list = list

   def get_matches(self, text):
      matches = []
      for x in self.list:
         if string.find(x, text) == 0:
            matches.append(x)
      return matches

#
# ui stuff
# the next sections of code contain UI widgets and plates used by the main keg
# class.
#
class plate_kegbot_main(plate_multi):
   def __init__(self,owner):
      plate_multi.__init__(self,owner)
      self.owner = owner

      self.maininfo, self.tempinfo, self.freezerinfo  = plate_std(owner), plate_std(owner), plate_std(owner)
      self.lastinfo, self.drinker  = plate_std(owner), plate_std(owner)

      line1 = widget_line_std("*------------------*",row=0,col=0,scroll=0)
      line2 = widget_line_std("|     kegbot!!     |",row=1,col=0,scroll=0)
      line3 = widget_line_std("| have good beer!! |",row=2,col=0,scroll=0)
      line4 = widget_line_std("*------------------*",row=3,col=0,scroll=0)

      self.maininfo.updateObject('line1',line1)
      self.maininfo.updateObject('line2',line2)
      self.maininfo.updateObject('line3',line3)
      self.maininfo.updateObject('line4',line4)

      line1 = widget_line_std("*------------------*",row=0,col=0,scroll=0)
      line2 = widget_line_std("| current temp:    |",row=1,col=0,scroll=0)
      line3 = widget_line_std("| unknown          |",row=2,col=0,scroll=0)
      line4 = widget_line_std("*------------------*",row=3,col=0,scroll=0)

      self.tempinfo.updateObject('line1',line1)
      self.tempinfo.updateObject('line2',line2)
      self.tempinfo.updateObject('line3',line3)
      self.tempinfo.updateObject('line4',line4)

      line1 = widget_line_std("*------------------*",row=0,col=0,scroll=0)
      line2 = widget_line_std("| freezer status:  |",row=1,col=0,scroll=0)
      line3 = widget_line_std("| [off]            |",row=2,col=0,scroll=0)
      line4 = widget_line_std("*------------------*",row=3,col=0,scroll=0)

      self.freezerinfo.updateObject('line1',line1)
      self.freezerinfo.updateObject('line2',line2)
      self.freezerinfo.updateObject('line3',line3)
      self.freezerinfo.updateObject('line4',line4)

      line1 = widget_line_std("*------------------*",row=0,col=0,scroll=0)
      line2 = widget_line_std("| last pour:       |",row=1,col=0,scroll=0)
      line3 = widget_line_std("| 0.0 oz           |",row=2,col=0,scroll=0)
      line4 = widget_line_std("*------------------*",row=3,col=0,scroll=0)

      self.lastinfo.updateObject('line1',line1)
      self.lastinfo.updateObject('line2',line2)
      self.lastinfo.updateObject('line3',line3)
      self.lastinfo.updateObject('line4',line4)

      line1 = widget_line_std("*------------------*",row=0,col=0,scroll=0)
      line2 = widget_line_std("| last drinker:    |",row=1,col=0,scroll=0)
      line3 = widget_line_std("| unknown          |",row=2,col=0,scroll=0)
      line4 = widget_line_std("*------------------*",row=3,col=0,scroll=0)

      self.drinker.updateObject('line1',line1)
      self.drinker.updateObject('line2',line2)
      self.drinker.updateObject('line3',line3)
      self.drinker.updateObject('line4',line4)

      self.addPlate("main",self.maininfo)
      self.addPlate("temp",self.tempinfo)
      self.addPlate("freezer",self.freezerinfo)
      self.addPlate("last",self.lastinfo)
      self.addPlate("drinker",self.drinker)

      # starts the rotation
      self.rotate_time = 5.0

   def setTemperature(self,tempc,tempf):
      inside = "%.1fc/%.1ff" % (tempc,tempf)
      line3 = widget_line_std("%s"%inside,row=2,col=0,prefix="| ", postfix= " |", scroll=0)
      self.tempinfo.updateObject('line3',line3)

   def setFreezer(self,status):
      inside = "[%s]" % status
      line3 = widget_line_std("%s"%inside,row=2,col=0,prefix="| ", postfix= " |", scroll=0)
      self.freezerinfo.updateObject('line3',line3)

   def setLastDrink(self,user,ounces):
      line3 = widget_line_std("%s oz"%ounces,row=2,col=0,prefix ="| ",postfix=" |",scroll=0)
      self.lastinfo.updateObject('line3',line3)
      line3 = widget_line_std("%s"%user,row=2,col=0,prefix ="| ",postfix=" |",scroll=0)
      self.drinker.updateObject('line3',line3)

# start a new kehbot instance, if we are called from the command line
if __name__ == '__main__':
   KegBot(config)