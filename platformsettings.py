#!/usr/bin/env python
# Copyright 2010 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import fileinput
import logging
import os
import platform
import re
import socket
import subprocess
import tempfile


class PlatformSettingsError(Exception):
  """Module catch-all error."""
  pass


class DnsReadError(PlatformSettingsError):
  """Raised when unable to read DNS settings."""
  pass


class DnsUpdateError(PlatformSettingsError):
  """Raised when unable to update DNS settings."""
  pass


def _check_output(*args):
  """Run Popen(*args) and return its output as a byte string.

  Python 2.7 has subprocess.check_output. This is essentially the same
  except that, as a convenience, all the positional args are used as
  command arguments.

  Args:
    *args: sequence of program arguments
  Raises:
    subprocess.CalledProcessError if the program returns non-zero exit status.
  Returns:
    output as a byte string.
  """
  command_args = [str(a) for a in args]
  logging.debug(' '.join(command_args))
  process = subprocess.Popen(command_args,
      stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
  output = process.communicate()[0]
  retcode = process.poll()
  if retcode:
    raise subprocess.CalledProcessError(retcode, command_args)
  return output


class PlatformSettings(object):
  _IPFW_BIN = None
  _IPFW_QUEUE_SLOTS = 100

  # Some platforms do not shape traffic with the loopback address.
  _USE_REAL_IP_FOR_TRAFFIC_SHAPING = False

  def __init__(self):
    self.original_primary_dns = None
    self.original_cwnd = None  # original TCP congestion window

  def get_primary_dns(self):
    raise NotImplementedError

  def _set_primary_dns(self):
    raise NotImplementedError

  def get_original_primary_dns(self):
    if self.original_primary_dns is None:
      self.original_primary_dns = self.get_primary_dns()
      logging.info('Saved original system DNS (%s)', self.original_primary_dns)
    return self.original_primary_dns

  def set_primary_dns(self, dns):
    self.get_original_primary_dns()
    self._set_primary_dns(dns)
    if self.get_primary_dns() == dns:
      logging.info('Changed system DNS to %s', dns)
    else:
      raise self._get_dns_update_error()

  def restore_primary_dns(self):
    if self.original_primary_dns is not None:
      self.set_primary_dns(self.original_primary_dns)
      self.original_primary_dns = None

  def get_cwnd(self):
    return None

  def _set_cwnd(self, args):
    pass

  def get_original_cwnd(self):
    if not self.original_cwnd:
      self.original_cwnd = self.get_cwnd()
    return self.original_cwnd

  def set_cwnd(self, cwnd):
    self.get_original_cwnd()
    self._set_cwnd(cwnd)
    if self.get_cwnd() == cwnd:
      logging.info("Changed cwnd to %s", cwnd)
    else:
      logging.error("Unable to update cwnd to %s", cwnd)

  def restore_cwnd(self):
    if self.original_cwnd is not None:
      self.set_cwnd(self.original_cwnd)
      self.original_cwnd = None

  def ipfw(self, *args):
    if self._IPFW_BIN:
      ipfw_args = [self._IPFW_BIN] + [str(a) for a in args]
      return _check_output(*ipfw_args)
    else:
      raise NotImplementedError

  def get_ipfw_queue_slots(self):
    return self._IPFW_QUEUE_SLOTS

  def get_server_ip_address(self, is_server_mode=False):
    """Returns the IP address to use for dnsproxy, httpproxy, and ipfw."""
    if is_server_mode or self._USE_REAL_IP_FOR_TRAFFIC_SHAPING:
      return socket.gethostbyname(socket.gethostname())
    return '127.0.0.1'

  def configure_loopback(self):
    """
    Configure loopback to be realistic.

    We use loopback for much of our testing, and on some systems, loopback
    behaves differently from real interfaces.
    """
    logging.error("Platform does not support loopback configuration.")

  def unconfigure_loopback(self):
    pass

  def get_system_logging_handler(self):
    """Return a handler for the logging module (optional)."""
    return None

  def ping(self, hostname):
    """Pings the hostname by calling the OS system ping command.
    Also stores the result internally.

    Args:
      hostname: hostname of the server to be pinged

    Returns:
      round trip time to the server in seconds, or 0 if unable to calculate RTT
    """
    raise NotImplementedError

class PosixPlatformSettings(PlatformSettings):
  _IPFW_BIN = 'ipfw'
  PING_PATTERN = r'rtt min/avg/max/mdev = \d+\.\d+/(\d+\.\d+)/\d+\.\d+/\d+\.\d+'

  def _get_dns_update_error(self):
    return DnsUpdateError('Did you run under sudo?')

  def _sysctl(self, *args):
    sysctl = subprocess.Popen(
        ['sysctl'] + [str(a) for a in args],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    stdout = sysctl.communicate()[0]
    return sysctl.returncode, stdout

  def has_sysctl(self, name):
    if not hasattr(self, 'has_sysctl_cache'):
      self.has_sysctl_cache = {}
    if name not in self.has_sysctl_cache:
      self.has_sysctl_cache[name] = self._sysctl(name)[0] == 0
    return self.has_sysctl_cache[name]

  def set_sysctl(self, name, value):
    rv = self._sysctl('%s=%s' % (name, value))[0]
    if rv != 0:
      logging.error("Unable to set sysctl %s: %s", name, rv)

  def get_sysctl(self, name):
    rv, value = self._sysctl('-n', name)
    if rv == 0:
      return value
    else:
      logging.error("Unable to get sysctl %s: %s", name, rv)
      return None

  def ping(self, hostname):
    """Pings the hostname by calling the OS system ping command.
    Also stores the result internally.

    Args:
      hostname: hostname of the server to be pinged

    Returns:
      round trip time to the server in milliseconds, or 0 if unavailable
    """
    rtt = 0
    try:
      # Regex matching may change depending on versions of ping? (unverified)
      proc = subprocess.Popen(['ping', '-c', '3', '-i', '0.2', hostname],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
      output, errors = proc.communicate()

      result = re.search(self.PING_PATTERN, output)
      if not result:
        logging.warning('Unable to ping %s: ', hostname)
      else:
        average = result.groups()
        rtt = float(average[0])
    except OSError, e:
      logging.critical('System error while trying to ping %s: %s', hostname, e)
    except ValueError, e:
      logging.critical('Subprocess called with invalid arguments: %s',e)
    return rtt


class OsxPlatformSettings(PosixPlatformSettings):
  LOCAL_SLOWSTART_MIB_NAME = 'net.inet.tcp.local_slowstart_flightsize'

  def _scutil(self, cmd):
    scutil = subprocess.Popen(
        ['scutil'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    return scutil.communicate(cmd)[0]

  def _ifconfig(self, *args):
    return _check_output('ifconfig', *args)

  def set_sysctl(self, name, value):
    rv = self._sysctl('-w', '%s=%s' % (name, value))[0]
    if rv != 0:
      logging.error("Unable to set sysctl %s: %s", name, rv)

  def _get_dns_service_key(self):
    # <dictionary> {
    #   PrimaryInterface : en1
    #   PrimaryService : 8824452C-FED4-4C09-9256-40FB146739E0
    #   Router : 192.168.1.1
    # }
    output = self._scutil('show State:/Network/Global/IPv4')
    lines = output.split('\n')
    for line in lines:
      key_value = line.split(' : ')
      if key_value[0] == '  PrimaryService':
        return 'State:/Network/Service/%s/DNS' % key_value[1]
    raise self._get_dns_update_error()

  def get_primary_dns(self):
    # <dictionary> {
    #   ServerAddresses : <array> {
    #     0 : 198.35.23.2
    #     1 : 198.32.56.32
    #   }
    #   DomainName : apple.co.uk
    # }
    output = self._scutil('show %s' % self._get_dns_service_key())
    primary_line = output.split('\n')[2]
    line_parts = primary_line.split(' ')
    return line_parts[-1]

  def _set_primary_dns(self, dns):
    command = '\n'.join([
      'd.init',
      'd.add ServerAddresses * %s' % dns,
      'set %s' % self._get_dns_service_key()
    ])
    self._scutil(command)

  def get_cwnd(self):
    return int(self.get_sysctl(self.LOCAL_SLOWSTART_MIB_NAME))

  def _set_cwnd(self, size):
    self.set_sysctl(self.LOCAL_SLOWSTART_MIB_NAME, size)

  def get_loopback_mtu(self):
    config = self._ifconfig('lo0')
    match = re.search(r'\smtu\s+(\d+)', config)
    if match:
      return int(match.group(1))
    return None

  def configure_loopback(self):
    """Configure loopback to use reasonably sized frames.

    OS X uses jumbo frames by default (16KB).
    """
    TARGET_LOOPBACK_MTU = 1500
    self.original_loopback_mtu = self.get_loopback_mtu()
    if self.original_loopback_mtu == TARGET_LOOPBACK_MTU:
      self.original_loopback_mtu = None
    if self.original_loopback_mtu is not None:
      self._ifconfig('lo0', 'mtu', TARGET_LOOPBACK_MTU)
      logging.debug('Set loopback MTU to %d (was %d)',
                    TARGET_LOOPBACK_MTU, self.original_loopback_mtu)
    else:
      logging.error('Unable to read loopback mtu. Setting left unchanged.')

  def unconfigure_loopback(self):
    if self.original_loopback_mtu is not None:
      self._ifconfig('lo0', 'mtu', self.original_loopback_mtu)
      logging.debug('Restore loopback MTU to %d', self.original_loopback_mtu)


class LinuxPlatformSettings(PosixPlatformSettings):
  """The following thread recommends a way to update DNS on Linux:

  http://ubuntuforums.org/showthread.php?t=337553

         sudo cp /etc/dhcp3/dhclient.conf /etc/dhcp3/dhclient.conf.bak
         sudo gedit /etc/dhcp3/dhclient.conf
         #prepend domain-name-servers 127.0.0.1;
         prepend domain-name-servers 208.67.222.222, 208.67.220.220;

         prepend domain-name-servers 208.67.222.222, 208.67.220.220;
         request subnet-mask, broadcast-address, time-offset, routers,
             domain-name, domain-name-servers, host-name,
             netbios-name-servers, netbios-scope;
         #require subnet-mask, domain-name-servers;

         sudo/etc/init.d/networking restart

  The code below does not try to change dchp and does not restart networking.
  Update this as needed to make it more robust on more systems.
  """
  RESOLV_CONF = '/etc/resolv.conf'
  TCP_INIT_CWND = 'net.ipv4.tcp_init_cwnd'
  TCP_BASE_MSS = 'net.ipv4.tcp_base_mss'
  TCP_MTU_PROBING = 'net.ipv4.tcp_mtu_probing'
  _IPFW_QUEUE_SLOTS = 500

  def get_primary_dns(self):
    try:
      resolv_file = open(self.RESOLV_CONF)
    except IOError:
      raise DnsReadError()
    for line in resolv_file:
      if line.startswith('nameserver '):
        return line.split()[1]
    raise DnsReadError()

  def _set_primary_dns(self, dns):
    """Replace the first nameserver entry with the one given."""
    try:
      self._write_resolve_conf(dns)
    except OSError, e:
      if 'Permission denied' in e:
        raise self._get_dns_update_error()
      raise

  def _write_resolve_conf(self, dns):
    is_first_nameserver_replaced = False
    # The fileinput module uses sys.stdout as the edited file output.
    for line in fileinput.input(self.RESOLV_CONF, inplace=1, backup='.bak'):
      if line.startswith('nameserver ') and not is_first_nameserver_replaced:
        print 'nameserver %s' % dns
        is_first_nameserver_replaced = True
      else:
        print line,
    if not is_first_nameserver_replaced:
      raise DnsUpdateError('Could not find a suitable nameserver entry in %s' %
                           self.RESOLV_CONF)

  def get_cwnd(self):
    if self.has_sysctl(self.TCP_INIT_CWND):
      return self.get_sysctl(self.TCP_INIT_CWND)
    else:
      return None

  def _set_cwnd(self, args):
    if self.has_sysctl(self.TCP_INIT_CWND):
      self.set_sysctl(self.TCP_INIT_CWND, str(args))


  def configure_loopback(self):
    """
    Linux will use jumbo frames by default (16KB), using the combination
    of MTU probing and a base MSS makes it use normal sized packets.

    The reason this works is because tcp_base_mss is only used when MTU
    probing is enabled.  And since we're using the max value, it will
    always use the reasonable size.  This is relevant for server-side realism.
    The client-side will vary depending on the client TCP stack config.
    """
    ENABLE_MTU_PROBING = 2
    TCP_FULL_MSS = 1460
    self.saved_tcp_mtu_probing = self.get_sysctl(self.TCP_MTU_PROBING)
    self.set_sysctl(self.TCP_MTU_PROBING, ENABLE_MTU_PROBING)
    self.saved_tcp_base_mss = self.get_sysctl(self.TCP_BASE_MSS)
    self.set_sysctl(self.TCP_BASE_MSS, TCP_FULL_MSS)

  def unconfigure_loopback(self):
    if self.saved_tcp_mtu_probing:
      self.set_sysctl(self.TCP_MTU_PROBING, self.saved_tcp_mtu_probing)
    if self.saved_tcp_base_mss:
      self.set_sysctl(self.TCP_BASE_MSS, self.saved_tcp_base_mss)

class WindowsPlatformSettings(PlatformSettings):
  _USE_REAL_IP_FOR_TRAFFIC_SHAPING = True

  def _get_dns_update_error(self):
    return DnsUpdateError('Did you run as administrator?')

  def _netsh_show_dns(self):
    """Return DNS information:

    Example output:
        Configuration for interface "Local Area Connection 3"
        DNS servers configured through DHCP:  None
        Register with which suffix:           Primary only

        Configuration for interface "Wireless Network Connection 2"
        DNS servers configured through DHCP:  192.168.1.1
        Register with which suffix:           Primary only
    """
    return _check_output('netsh', 'interface', 'ip', 'show', 'dns')

  def get_primary_dns(self):
    match = re.search(r':\s+(\d+\.\d+\.\d+\.\d+)', self._netsh_show_dns())
    return match and match.group(1) or None

  def _set_primary_dns(self, dns):
    vbs = """
Set objWMIService = GetObject( _
   "winmgmts:{impersonationLevel=impersonate}!\\\\.\\root\\cimv2")
Set colNetCards = objWMIService.ExecQuery( _
    "Select * From Win32_NetworkAdapterConfiguration Where IPEnabled = True")
For Each objNetCard in colNetCards
  arrDNSServers = Array("%s")
  objNetCard.SetDNSServerSearchOrder(arrDNSServers)
Next
""" % dns
    vbs_file = tempfile.NamedTemporaryFile(suffix='.vbs', delete=False)
    vbs_file.write(vbs)
    vbs_file.close()
    subprocess.check_call(['cscript', '//nologo', vbs_file.name])
    os.remove(vbs_file.name)

  def _arp(self, *args):
    return _check_output('arp', *args)

  def _route(self, *args):
    return _check_output('route', *args)

  def _ipconfig(self, *args):
    return _check_output('ipconfig', *args)

  def get_mac_address(self, ip):
    """Return the MAC address for the given ip."""
    ip_re = re.compile(r'^\s*IP(?:v4)? Address[ .]+:\s+([0-9.]+)')
    for line in self._ipconfig('/all').splitlines():
      if line[:1].isalnum():
        current_ip = None
        current_mac = None
      elif ':' in line:
        line = line.strip()
        ip_match = ip_re.match(line)
        if ip_match:
          current_ip = ip_match.group(1)
        elif line.startswith('Physical Address'):
          current_mac = line.split(':', 1)[1].lstrip()
        if current_ip == ip and current_mac:
          return current_mac
    return None

  def configure_loopback(self):
    self.ip = self.get_server_ip_address()
    self.mac_address = self.get_mac_address(self.ip)
    if self.mac_address:
      self._arp('-s', self.ip, self.mac_address)
      self._route('add', self.ip, self.ip, 'mask', '255.255.255.255')
    else:
      logging.warn('Unable to configure loopback: MAC address not found.')
    # TODO(slamm): Configure cwnd, MTU size

  def unconfigure_loopback(self):
    if self.mac_address:
      self._arp('-d', self.ip)
      self._route('delete', self.ip, self.ip, 'mask', '255.255.255.255')

  def get_system_logging_handler(self):
    """Return a handler for the logging module (optional).

    For Windows, output can be viewed with DebugView.
    http://technet.microsoft.com/en-us/sysinternals/bb896647.aspx
    """
    import ctypes
    output_debug_string = ctypes.windll.kernel32.OutputDebugStringA
    output_debug_string.argtypes = [ctypes.c_char_p]
    class DebugViewHandler(logging.Handler):
      def emit(self, record):
        output_debug_string("[wpr] " + self.format(record))
    return DebugViewHandler()


class WindowsXpPlatformSettings(WindowsPlatformSettings):
  _IPFW_BIN = r'third_party\ipfw_win32\ipfw.exe'


def _new_platform_settings():
  """Make a new instance of PlatformSettings for the current system."""
  system = platform.system()
  release = platform.release()
  if system == 'Darwin':
    return OsxPlatformSettings()
  if system == 'Linux':
    return LinuxPlatformSettings()
  if system == 'Windows':
    if release == 'XP':
      return WindowsXpPlatformSettings()
    else:
      return WindowsPlatformSettings()
  raise NotImplementedError('Sorry %s %s is not supported.' % (system, release))

_platform_settings = None
def get_platform_settings():
  """Return a single instance of PlatformSettings."""
  global _platform_settings
  if not _platform_settings:
    _platform_settings = _new_platform_settings()
  return _platform_settings
