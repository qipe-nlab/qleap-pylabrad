# Copyright (C) 2007  Matthew Neeley
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
labrad.node


Provides an interface to manage all python labrad servers
running on a particular computer.  This version runs each
server in a separate process, so that they can not interfere
with each other.  Information such as the manager host, port
and password are passed to the child process in environment
variables or via command line arguments.  The startup process
for each child server is controlled by an associated .ini file.
"""

import os
import re
import sys
from datetime import datetime
from ConfigParser import SafeConfigParser

import labrad
from labrad import util, types as T
from labrad.server import ILabradServer, LabradServer, setting
from labrad.util import dispatcher, findEnvironmentVars, interpEnvironmentVars

from twisted.application.service import IService, MultiService
from twisted.application.internet import TCPClient
from twisted.internet import defer, reactor
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.internet.protocol import ProcessProtocol
from twisted.internet.error import ProcessDone, ProcessTerminated
from twisted.python import log, failure
from twisted.python.components import registerAdapter
from twisted.plugin import getPlugins

from zope.interface import Interface, implements

LOG_LENGTH = 1000 # maximum number of lines of stdout to keep per server

class IServerProcess(Interface):
    pass
    
class ServerProcess(ProcessProtocol):
    """A class to represent a running server instance."""
    implements(IServerProcess)
    timeout = 20

    def __init__(self, env):
        self.env = os.environ.copy()
        self.env.update(env)
        cls = self.__class__
        self.name = interpEnvironmentVars(cls.instancename, self.env)
        self.cmdline = interpEnvironmentVars(cls.cmdline, self.env)
        self.args = self.cmdline.split()
        self.executable = os.path.join(self.path, self.args[0])
        # TODO: easier cmdline customization (different args, path, exe, etc.)
        self.starting = False
        self.running = False
        self.stopping = False
        self.output = []
        self._lock = defer.DeferredLock()
            
    @property
    def status(self):
        if self.starting:
            return 'STARTING'
        elif self.running:
            return 'RUNNING'
        elif self.stopping:
            return 'STOPPING'
        else:
            return 'STOPPED'
        
    def start(self):
        """Start this server instance."""
        return self._lock.run(self._start)
    
    def stop(self):
        """Stop this server instance."""
        return self._lock.run(self._stop)
    
    def restart(self):
        """Restart this server instance."""
        return self._lock.run(self._restart)
    
    @inlineCallbacks
    def _start(self):
        if self.running:
            return
        print "starting '%s'..." % self.name
        print "path:", self.path
        print "executable:", self.executable
        print "args:", self.args
        self.starting = True
        self.startup = defer.Deferred()
        dispatcher.connect(self.serverConnected, 'serverConnected')
        self.emitMessage('server_starting')
        self.proc = reactor.spawnProcess(self, self.executable, self.args,
                                         env=self.env, path=self.path)
        timeoutCall = reactor.callLater(self.timeout, self.kill)
        try:
            yield self.startup
            self.emitMessage('server_started')
        except:
            self.emitMessage('server_stopped')
            raise
        finally:
            self.starting = False
            if timeoutCall.active():
                timeoutCall.cancel()
            dispatcher.disconnect(self.serverConnected, 'serverConnected')
        
    @inlineCallbacks
    def _stop(self):
        if not self.running:
            return
        print "stopping '%s'..." % self.name
        self.stopping = True
        self.shutdown = defer.Deferred()
        self.emitMessage('server_stopping')
        self.kill()
        yield self.shutdown
        self.stopping = False
        self.emitMessage('server_stopped')
        
    @inlineCallbacks
    def _restart(self):
        yield self._stop()
        yield self._start()
        
    def emitMessage(self, msg):
        """Emit a message to other parts of this application."""
        dispatcher.send(msg, sender=self, server=self.name)
        
    def serverConnected(self, ID, name):
        """Called when a server connects to LabRAD.
        
        If the name matches our name, we'll assume this server
        started successfully.  This may not be the case (e.g. if
        two nodes are trying to start the same server simultaneously),
        but there's no way to find out from LabRAD which node
        a given server is running on, so this will have to do.
        """
        if name == self.name:
            self.ID = ID
            self.running = True
            self.startup.callback(self)

    def processEnded(self, reason):
        """Called when the server process ends.
        
        We check to see the reason why this process failed, and then
        call the appropriate deferred, depending on the current state.
        """
        if isinstance(reason.value, ProcessDone):
            print "'%s': process closed cleanly." % self.name
        elif isinstance(reason.value, ProcessTerminated):
            print "'%s': process terminated: %s" % (self.name, reason.value)
        else:
            print "'%s': process ended: %s" % (self.name, reason)
        self.running = False
        if self.starting:
            err = T.Error('Startup failed.', payload=self.output)
            self.startup.errback(err)
        elif self.stopping:
            self.shutdown.callback(None)
        else:
            # looks like this thing died on its own
            self.emitMessage('server_stopped')
    
    @inlineCallbacks
    def kill(self):
        """Kill the server process."""
        if not self.running:
            return
        try:
            # first try to shutdown nicely with a message
            servers = self.client.servers
            if self.name in servers:
                servers[self.name].sendMessage(987654321)
                yield util.wakeupCall(10.0)
                
            # if we're not dead yet, kill with a vengeance
            if self.running:
                self.proc.signalProcess('KILL')
        except:
            msg = 'Error while trying to kill server process for "%s":'
            print kills, msg % self.name
            failure.Failure().printTraceback(elideFrameworkCode=1)
        
    def outReceived(self, data):
        """Called when the server prints to stdout."""
        self.output.append((datetime.now(), data))
        self.output = self.output[-LOG_LENGTH:]
        #print "'%s' stdout: %s" % (self.name, data)

    def errReceived(self, data):
        """Called when the server prints to stderr."""
        self.output.append((datetime.now(), data))
        self.output = self.output[-LOG_LENGTH:]
        #print "'%s' stderr: %s" % (self.name, data)

    def clearOutput(self):
        """Clear the log of stdout."""
        self.output = []

def createGenericServerCls(path, filename):
    """Create a ServerProcess class representing a generic server.
    
    Options for this server are read from a .ini config file on disk.
    """
    class cls(ServerProcess):
        pass
    
    scp = SafeConfigParser()
    scp.read(os.path.join(path, filename))

    # general information
    cls.name = scp.get('info', 'name', raw=True)
    cls.__doc__ = scp.get('info', 'description', raw=True)
    if scp.has_option('info', 'version'):
        cls.version = scp.get('info', 'version', raw=True)
    else:
        cls.version = '0.0'
    try:
        cls.instancename = scp.get('info', 'instancename', raw=True)
    except:
        cls.instancename = cls.name
    cls.environVars = findEnvironmentVars(cls.instancename)
    cls.isLocal = len(cls.environVars) > 0

    # startup
    cls.cmdline = scp.get('startup', 'cmdline', raw=True)
    cls.path = path
    try:
        cls.timeout = float(scp.getint('startup', 'timeout'))
    except:
        pass

    return cls

def createPythonServerCls(plugin):
    """Create a ServerProcess class representing a python server.
    
    Options for this server are read from the python object itself,
    located with the twisted plugin system.
    """
    class cls(ServerProcess):
        pass

    # general information
    cls.name = plugin.name
    cls.__doc__ = plugin.__doc__
    if hasattr(plugin, 'version'):
        cls.version = plugin.version
    else:
        cls.version = '0.0'
    if hasattr(plugin, 'instanceName'):
        cls.instancename = plugin.instanceName
    else:
        cls.instancename = plugin.name
    cls.environVars = findEnvironmentVars(cls.instancename)
    cls.isLocal = len(cls.environVars) > 0
    
    # startup
    cls.cmdline = ' '.join([sys.executable, '-m', plugin.__module__])
    cls.path = os.path.split(sys.modules[plugin.__module__].__file__)[0]
    return cls

class Node(MultiService):
    """Parent Service that keeps the node running.
    
    If the manager is stopped or we lose the network connection,
    this service attempts to restart it so that we will come
    back online when the manager is back up.
    """
    reconnectDelay = 10

    def __init__(self, name, host, port):
        MultiService.__init__(self)
        self.name = name
        self.host = host
        self.port = port

    def startService(self):
        MultiService.startService(self)
        self.startConnection()

    def startConnection(self):
        """Attempt to start the node and connect to LabRAD."""
        print 'Connecting to %s:%d...' % (self.host, self.port)
        node = NodeServer(self.name, self.host, self.port)
        node.onStartup().addErrback(self._error)
        node.onShutdown().addCallbacks(self._disconnected, self._error)
        self.cxn = TCPClient(self.host, self.port, node)
        self.addService(self.cxn)

    def _disconnected(self, data):
        print 'Node disconnected from manager.'
        return self._reconnect()

    def _error(self, failure):
        print failure.getErrorMessage()
        return self._reconnect()

    def _reconnect(self):
        """Clean up from the last run and reconnect."""
        ## hack: manually clearing the dispatcher...
        dispatcher.connections.clear()
        dispatcher.senders.clear()
        dispatcher._boundMethods.clear()
        ## end hack
        if hasattr(self, 'cxn'):
            self.removeService(self.cxn)
            del self.cxn
        reactor.callLater(self.reconnectDelay, self.startConnection)
        print 'Will try to reconnect in %d seconds...' % self.reconnectDelay

class NodeConfig(object):
    """Load configuration from the registry and monitor it for changes."""
    # TODO: add to config: autostarting, refreshinterval, preferred start location
        
    @classmethod
    @inlineCallbacks
    def create(cls, cxn, nodename):
        """Loads node configuration from the registry."""
        instance = cls(cxn, nodename)
        yield instance._init()
        returnValue(instance)
    
    def __init__(self, cxn, nodename):
        self._cxn = cxn
        self._reg = cxn.registry
        self._ctx = cxn.context()
        self.nodename = nodename
        
    @inlineCallbacks
    def _init(self):
        """Initialize by loading from the registry.
        
        Copy from the default directory, creating it if necessary.
        Also, set up messages to monitor the config directory for
        changes.
        """
        p = self._packet()
        p.cd(['', 'Nodes'], True)
        p.dir()
        ans = yield p.send()
        dirs, keys = ans.dir
        
        # load defaults (creating them if necessary)
        create = '__default__' not in dirs
        defaults = ([], ['labrad.servers'])
        defaults = yield self._load('__default__', create, defaults)
        
        # load this node (creating config if necessary)
        create = self.nodename not in dirs
        config = yield self._load(self.nodename, create, defaults)
        self._update(config)
        
        # setup messages when registry changes
        self._reg.addListener(self._handleMessage, context=self._ctx)
        p = self._packet()
        p.notify_on_change(2345, True)
        yield p.send()
    
    def _packet(self):
        """Create a packet to the registry server in our context."""
        return self._reg.packet(context=self._ctx)
    
    def _update(self, config):
        """Update instance variables from loaded config."""
        self.dirs, self.mods = config
        print 'config updated:', self.dirs, self.mods
        
    @inlineCallbacks
    def _load(self, nodename=None, create=False, defaults=None):
        """Load the current configuration out of the registry."""
        p = self._packet()
        if nodename is not None:
            p.cd(['', 'Nodes', nodename], True)
        if create:
            p.set('directories', defaults[0])
            p.set('packages', defaults[1])
        p.get('directories', '*s', key='dirs')
        p.get('packages', '*s', key='mods')
        ans = yield p.send()
        dirs = filter(None, ans.dirs)
        mods = filter(None, ans.mods)
        returnValue((dirs, mods))
        
    def _save(self):
        """Save the current configuration to the registry."""
        p = self._packet()
        p.set('directories', self.dirs)
        p.set('packages', self.mods)
        return p.send()
        
    @inlineCallbacks
    def _handleMessage(self, c, msg):
        """Reload when we get a message from the registry."""
        config = yield self._load()
        self._update(config)


class NodeServer(LabradServer):
    """Start and stop LabRAD servers remotely.
    
    The node server allows you to control and
    monitor servers running on a remote machine.
    """
    
    name = 'node %LABRADNODE%'

    def __init__(self, nodename, host, port):
        LabradServer.__init__(self)
        self.nodename = nodename
        self.name = 'node %s' % nodename
        self.host = host
        self.port = port

    @inlineCallbacks
    def initServer(self):
        """Initialize this server."""
        self.servers = {}
        self.instances = {}
        self.starters = {}
        self.runners = {}
        self.stoppers = {}
        self.initMessages(True)
        self.config = yield NodeConfig.create(self.client, self.nodename)
        self.refreshLock = defer.DeferredLock()
        yield self.refreshServers()
    
    def stopServer(self):
        """Stop this node by killing all subprocesses."""
        self.initMessages(False)
        stoppages = [srv.stop() for srv in self.runners.values()]
        return defer.DeferredList(stoppages)
        
        
    # message handling
        
    def initMessages(self, connect=True):
        """Set up message dispatching."""
        f = getattr(dispatcher, 'connect' if connect else 'disconnect')
        # set up messages to be relayed out over LabRAD
        messages = ['server_starting', 'server_started',
                    'server_stopping', 'server_stopped',
                    'list_updated']
        for message in messages:
            f(self._relayMessage, message)
        # set up message handlers for subprocess events
        f(self.subprocessStarting, 'server_starting')
        f(self.subprocessStarted, 'server_started')
        f(self.subprocessStopping, 'server_stopping')
        f(self.subprocessStopped, 'server_stopped')

    def _relayMessage(self, signal, sender, **kw):
        """Send messages out to LabRAD."""
        kw['node'] = self.name
        mgr = self.client.manager
        mgr.send_named_message('node.' + signal, tuple(kw.items()))

    def serverConnected(self, ID, name):
        """Called when a server connects to LabRAD."""
        dispatcher.send('serverConnected', ID=ID, name=name)
        
    def subprocessStarting(self, sender):
        """Called when a subprocess begins connecting."""
        self.starters[sender.name] = sender

    def subprocessStarted(self, sender):
        """Called when a subprocess successfully connects."""
        if sender.name in self.starters:
            del self.starters[sender.name]
        self.runners[sender.name] = sender

    def subprocessStopping(self, sender):
        """Called when a subprocess successfully disconnects."""
        if sender.name in self.runners:
            del self.runners[sender.name]
        self.stoppers[sender.name] = sender

    def subprocessStopped(self, sender):
        """Called when a subprocess successfully disconnects."""
        if sender.name in self.runners:
            del self.runners[sender.name]
        if sender.name in self.stoppers:
            del self.stoppers[sender.name]
            

    # server refresh
            
    def refreshServers(self):
        """Refresh the list of available servers."""
        self.servers = servers = {}
        # look for python plugins
        for module in self.config.mods:
            try:
                __import__(module)
                plugins = getPlugins(ILabradServer, sys.modules[module])
                for plugin in plugins:
                    s = createPythonServerCls(plugin)
                    s.client = self.client
                    if s.name not in servers:
                        servers[s.name] = s
            except:
                print 'Error while loading plugins from module "%s":' % module
                failure.Failure().printTraceback(elideFrameworkCode=1)
        # look for .ini files
        for dirname in self.config.dirs:
            for path, dirs, files in os.walk(dirname):
                for f in files:
                    if not f.lower().endswith('.ini'):
                        continue
                    try:
                        s = createGenericServerCls(path, f)
                        s.client = self.client
                        if s.name not in servers:
                            servers[s.name] = s
                    except:
                        fname = os.path.join(path, f)
                        print 'Error while loading config file "%s":' % fname
                        failure.Failure().printTraceback(elideFrameworkCode=1)
        # TODO: send a message with the current server list
        #dispatcher.send('refreshed', servers=self.servers_info(None))        
    
    
    # LabRAD settings
    
    @setting(1, name='s', environ='*(ss)', returns='s')
    def start(self, c, name, environ={}):
        """Start an instance of a server."""
        if name not in self.servers:
            raise Exception("Unknown server: '%s'." % name)
        environ = dict(environ) # copy context environment
        environ.update(LABRADNODE=self.nodename,
                       LABRADHOST=self.host,
                       LABRADPORT=str(self.port),
                       LABRADPASSWORD=self.password)
        srv = self.servers[name](environ)
        # TODO: check whether an instance with this name already exists
        self.instances[name] = srv
        yield srv.start()
        returnValue(srv.name)

    @setting(2, name='s', returns='s')
    def stop(self, c, name):
        """Stop a running server instance."""
        if name not in self.runners:
            raise Exception("'%s' is not running." % name)
        srv = self.runners[name]
        yield srv.stop()
        returnValue(srv.name)

    @setting(3, name='s', returns='s')
    def restart(self, c, name):
        """Restart a running server instance."""
        if name not in self.runners:
            raise Exception("'%s' is not running." % name)
        srv = self.runners[name]
        yield srv.restart()
        returnValue(srv.name)

    @setting(10, returns='*s')
    def available_servers(self, c):
        """Get a list of available servers."""
        return sorted(self.servers.keys())

    @setting(11, returns='*(ss)')
    def running_servers(self, c):
        """Get a list of running server instances.

        Returns a list of tuples of server name and instance name.
        """
        return sorted((s.__class__.name, n) for n, s in self.runners.items())

    @setting(12, returns='*s')
    def local_servers(self, c):
        """Get a list of local servers."""
        return sorted(n for n, s in self.servers.items() if s.isLocal)

    @setting(13, returns='')
    def refresh_servers(self, c):
        """Refresh the list of available servers."""
        yield self.refreshServers()

    @setting(14, returns='*(s{name} s{desc} s{ver} s{instname} *s{vars} *s{running})')
    def servers_info(self, c):
        """Get information about all servers on this node."""
        return [self.serverInfo(item[1])
                for item in sorted(self.servers.items())]

    def serverInfo(self, cls):
        """Get information about a particular server on this node."""
        instances = [n for n, s in self.runners.items()
                                if s.__class__.name == cls.name]
        return (cls.name, cls.__doc__ or '', cls.version,
                cls.instancename, cls.environVars, instances)

    @setting(100, name='s', returns='*(ts)')
    def server_output(self, c, name):
        """Get output from a server's stdout."""
        if name not in self.runners:
            raise Exception("'%s' is not running." % name)
        return self.runners[name].output

    @setting(101, name='s', returns='')
    def clear_output(self, c, name):
        """Clear the stdout buffer of a server."""
        if name not in self.runners:
            raise Exception("'%s' is not running." % name)
        self.runners[name].clearOutput()

    @setting(102, name='s', returns='s')
    def server_version(self, c, name):
        """Get version information for a server."""
        if name not in self.servers:
            raise Exception("'%s' not found." % name)
        return self.servers[name].version
    
    @setting(1000, returns='*(ss)')
    def node_version(self, c):
        """Return a list of key-value tuples containing info about this node."""
        import socket, sys
        info = {
            'hostname': socket.gethostname(),
            'nodename': self.nodename,
            'python version': sys.version,
            'labrad version': labrad.__version__,
            'labrad revision': labrad.__revision__,
            'labrad date': labrad.__date__,
            }
        return list(info.items())
