import argparse
import logging
import yaml
import os
import sys
import docker
import os.path
import netaddr
import subprocess
from pyroute2 import IPDB
from pyroute2 import netns
from packaging import version
if sys.platform != 'darwin':
  from pyroute2 import NetNS
from distutils.version import LooseVersion
import networkx as nx
from networkx.readwrite import json_graph

import json
# Global constants
LOG = logging.getLogger(__name__)
DOCKER = docker.from_env()
netns.NETNS_RUN_DIR = "/var/run/docker/netns"
SUPPORTED_DRIVERS = ['macvlan', 'bridge', 'veth']
DEVNULL = open(os.devnull, 'w')
CVP_MEM_MAX = "8192" 

# Global variables
VERSION = 1
CONF_DIR = './config'
PUBLISH_BASE = 8000
OOB_PREFIX = '192.168.100.0/24'
PREFIX = 'CEOS-LAB'
VMX_VERSION = 'vrnetlab/vr-vmx:17.2R1.13'
CSR_VERSION = 'vrnetlab/vr-csr:16.04.01'
XRV_VERSION = 'vrnetlab/vr-xrv:6.1.2'
CUSTOM_IMAGE = dict()
CVP_VERSION = 'cvp:latest'

def parse_args():
    parser = argparse.ArgumentParser(description="Tool to create docker topologies")
    parser.add_argument(
        '-d', '--debug',
        help='Enable Debug',
        action='store_true'
    )
    
    m_group = parser.add_argument_group(title="Actions", description="Create or destroy topology")
    main_group = m_group.add_mutually_exclusive_group()
    main_group.add_argument(
        "--create",
        help="Create topology",
        action="store_true"
    )
    main_group.add_argument(
        "--destroy",
        help="Destroy topology",
        action="store_true"
    )
    main_group.add_argument("--graph", help="Generate a D3 graph", action="store_true")

    save_group = parser.add_argument_group(title="Save", description="Save or archive the topology")
    save_group.add_argument(
        "-s", "--save",
        help="Save topology configs",
        action="store_true"
    )
    save_group.add_argument(
        "-a", "--archive",
        help="Archive topology file and configs",
        action="store_true"
    )

    parser.add_argument(
        "topology",
        help='Topology file',
        type=str
    )

    args = parser.parse_args()
    return args


def create_d3_graph(devices, links):
    g = nx.Graph()
    for link in links:
        for i in range(len(link.endpoints)):

            link.endpoints[i]=(link.endpoints[i][0].split("_")[-1],link.endpoints[i][1],link.endpoints[i][2],link.endpoints[i][3])
    devices_running = [
        device for  device in devices.items() if len(device) > 0
    ]
    devices_sorted = sorted([device.lower() for device, _ in devices.items()])
    g.add_nodes_from([(devices_sorted.index(d), dict(name=d)) for d in devices_sorted])
    [
        g.add_edge(
            devices_sorted.index(link.endpoints[0][0]),
            devices_sorted.index(link.endpoints[1][0]),
            value=1,
        )
        for link in links
        if len(link.endpoints) == 2
    ]
    device_nodes ={d[0]:'node1' for d in devices_running if d}
    # faking device nodes
    # device_nodes = {'host-1': 'node1', 'host-2': 'node1', 'host-3': 'node3'}
    unique_nodes = sorted(list(set(device_nodes.values())))
    groups = {
        devices_sorted.index(device): unique_nodes.index(node)
        for device, node in device_nodes.items()
    }
    nx.set_node_attributes(g, groups, "group")
    cwd = os.getcwd()
    filepath = os.path.join(cwd, "/web/graph.json")
    with open(filepath, "w") as f:
        f.write(json.dumps(json_graph.node_link_data(g), indent=2))


def run_cmd(command, sudo=False):
    if sudo:
        sudo_prefix = 'sudo '
    else:
        sudo_prefix = ''
    cmd = sudo_prefix + command
    LOG.debug("Running command: {}".format(cmd))
    output = None if LOG.getEffectiveLevel() == logging.DEBUG else DEVNULL
    return subprocess.call(cmd, shell=True, stdout=output, stderr=output)

def write_file(filename, text):
    if os.path.exists(filename):
        LOG.debug("Filename {} exists, overwriting".format(filename))
    else:
        LOG.debug("Creating new file {}".format(filename))
    with open(filename, 'w') as f:
        f.write(text)

def archive_topo(confdir, topofile):
    LOG.debug("Archiving {} and {}".format(confdir, topofile))
    outfile = os.path.splitext(topofile)[0] + '.tar.gz'
    confdir = os.path.basename(confdir)
    import tarfile
    with tarfile.open(outfile, 'w:gz') as tar:
        tar.add(confdir)
        tar.add(topofile)
    return outfile

def enable_lldp(networks):
    for each in networks:
        try:
            cmd = 'echo 16384 > /sys/class/net/br-{}/bridge/group_fwd_mask'.format(each.network.id[:12])
            run_cmd(cmd, True)
        except:
            continue
    return

def enable_ipForwarding(devices):
    [d.container.exec_run("sysctl -w net.ipv4.ip_forward=1") for _,d in devices.items() if d.type == 'ceos']

def enable_write_mem(devices):
    cmd = "sed -i 's/os.rename( srcFileName, dstFileName )/import shutil;shutil.move( srcFileName, dstFileName )/' /usr/lib/python2.7/site-packages/Url.py"
    [d.container.exec_run(cmd) for _,d in devices.items() if d.type == 'ceos']

def enable_bridge_forwarding():
    #cmd = 'sysctl net.bridge.bridge-nf-call-iptables=0'
    check_cmd = 'iptables -C DOCKER-ISOLATION-STAGE-1 -j ACCEPT'
    add_cmd = 'iptables -I DOCKER-ISOLATION-STAGE-1 -j ACCEPT'
    while run_cmd(check_cmd, True) == 0:
        cleanup_bridge_forwarding()
    run_cmd(add_cmd, True)
    return

def cleanup_bridge_forwarding():
    #cmd = 'sysctl net.bridge.bridge-nf-call-iptables=0'
    check_cmd = 'iptables -C DOCKER-ISOLATION-STAGE-1 -j ACCEPT'
    delete_cmd = 'iptables -D DOCKER-ISOLATION-STAGE-1 -j ACCEPT'
    if run_cmd(check_cmd, True) == 0:
        run_cmd(delete_cmd, True)
    return

def kill_agetty():
    cmd = 'pkill agetty'
    run_cmd(cmd, True)
    return


def parse_endpoints(devices, endpoints, link, idx):
    """
    Parses a single endpoints list in the format:
    ["Device-A:Interface-1", "cvp-1", "host-1", "host-2:Eth1", "host-3:Eth2:192.168.10.30/24"]
    Updates devices dictionary
    """
    for endpoint in endpoints:
        # Example: ep == "host-3:Eth2:192.168.10.30/24"
        ep = endpoint.split(':')
        # Example: ep == ["host-3", "Eth2", "192.168.10.30/24"]
        if not ep:
            LOG.error('Link contains empty definition')
        device_name = ep.pop(0)
        if len(ep) > 1:
            interface=ep[0]
            ip = ep.pop()
        else:
            ip = ""        
        # Example: device_name == "host-3"
        # Match device image based on prefix
        if 'cvp' in device_name.lower(): # The below is dict.get(key, default) 
            device = devices.get(device_name, CVP(device_name))
        elif 'veos' in device_name.lower():
            device = devices.get(device_name, VEOS(device_name))
        elif 'vmx' in device_name.lower():
            device = devices.get(device_name, VMX(device_name))
        elif 'csr' in device_name.lower():
            device = devices.get(device_name, CSR(device_name))
        elif 'xrv' in device_name.lower():
            device = devices.get(device_name, XRV(device_name))
        elif any(image in device_name.lower() for image in CUSTOM_IMAGE.keys()):
            image_name = [CUSTOM_IMAGE[image] for image in CUSTOM_IMAGE.keys() if image in device_name.lower()][0]
            device = devices.get(device_name, Generic(device_name, image=image_name))
        elif 'host' in device_name.lower():
            # Example: ep == ["Eth2","192.168.10.30/24"]
            device = devices.get(device_name, Host(device_name))
            if len(ep) > 1:
                # This means we have intf name _AND_ IP address
                ip = ep.pop()
                device.add_ip(ip, ep[0])            
        else:
            # This creates default CEOS device type
            device = devices.get(device_name, CEOS(device_name))
        if ep:
            int_name = ep.pop()
        else:
            # ep is None, meaning endpoint was defined without
            # interface name, like "host-2"
            int_name = 'eth{}'.format(idx)
        if ip:
            device.add_ip(ip,interface)
        # Remember device connections
        device.connect(int_name, link)
        devices[device_name] = device   

def parse_v1(t_yml):
    """
    Reads the topology definition in the following format:
    links:
      - ["Device-A:Interface-1", "Device-B:Interface-3"]
      - ["Device-A:Interface-1", "cvp-1", "host-1", "host-2:Eth1", "host-3:Eth2:192.168.10.30/24"]
    Returns a dict of devices and a list links
    """
    devices = dict()
    links = list()
    for idx, link_dsc in enumerate(t_yml['links']):
        # Example: link_dsc == ["Device-A:Interface-1", "cvp-1", "host-1", "host-3:Eth2:192.168.10.30/24"]
        if len(link_dsc) == 2:
            # Example: link_dsc == ["Device-A:Interface-1", "cvp-1"]
            link_type = 'p2p'
        else:
            # Example: link_dsc == ["Device-A:Interface-1", "cvp-1", "host-1"]
            link_type = 'mpoint'
        link = Link(link_type, 'net-{}'.format(idx))
        links.append(link)
        parse_endpoints(devices, link_dsc, link, idx)
    return devices, links

def parse_v2(t_yml):
    """
    Reads the topology definition in the following format:
    links:
      - driver: macvlan (Optional)
        driver_opts: (Optional)
          parent: eth0
        endpoints: ["Device-A:Interface-1", "Device-B:Interface-3"]
      - endpoints:
          - Device-A:Interface-1
          - cvp-1
          - host-1
          - host-2:Eth1
          - host-3:Eth2:192.168.10.30/24
    Returns a dict of devices and a list of links
    """
    devices = dict()
    links = list()
    for idx, link_dsc in enumerate(t_yml['links']):
        # Example: link_dsc == {'driver': 'macvlan', 'driver_opts': {'parent': 'eth1'},
        # endpoints: ["Device-A:Interface-1", "cvp-1", "host-1", "host-3:Eth2:192.168.10.30/24"]}
        LOG.debug("parsing link {}".format(link_dsc))
        # Link-specific driver takes priority over topology-specific 
        link_driver = link_dsc.get('driver', t_yml.get('driver', None))
        driver_opts = link_dsc.get('driver_opts', None)
        link_endpoints = link_dsc.get('endpoints', None)
        if not link_endpoints:
            LOG.error("Missing endpoints definition")
        else:
            LOG.debug("parsing endpoints {}".format(link_endpoints))
        if len(link_endpoints) <= 2:
            # Example: link_dsc == ["Device-A:Interface-1", "cvp-1"]
            link_type = 'p2p'
        else:
            # Example: link_dsc == ["Device-A:Interface-1", "cvp-1", "host-1"]
            link_type = 'mpoint'
        link = Link(link_type, 'net-{}'.format(idx), link_driver, driver_opts)
        links.append(link)
        parse_endpoints(devices, link_endpoints, link, idx)
    return devices, links

class Device(object):
    def __init__(self, name):
        LOG.debug('\tConstructing device {}'.format(name))
        self.name = '_'.join([PREFIX, name])
        self.hostname = name
        # Setting up defaults
        self.type = ''
        self.image = ''
        self.command = ''
        self.environment = dict()
        self.pid = None
        self.sandbox = None
        self.default_network = None
        self.start_mode = None
        self.sysctls = {'net.ipv4.ip_forward': 1}
        self.entry_cmd = 'docker exec -it {} sh'.format(self.name)
        # Setting up extra variables
        self.interfaces = dict()
        self.volumes = dict()
        self.ports = dict()
        self.ips=dict()
        # Pointer to docker SDK object
        self.container = None

    def _update_start_mode(self, interface, link):
        new_start_mode = 'manual' if link.driver == 'veth' else 'auto'
        LOG.debug("\tUpdating start_mode from {} to {}".format(self.start_mode, new_start_mode))
        if not self.start_mode:
            self.start_mode = new_start_mode
            if self.start_mode == 'manual':
                self.default_network = 'none'
            else:
                # Popping the current interface from the list 
                self.interfaces.pop(interface)
                # Default network now points to the new link
                self.default_network = link.name
            LOG.debug("\tDefault network is set to {}".format(self.default_network))
        elif self.start_mode == 'auto' and new_start_mode == 'manual':
            self.start_mode = 'manual'
        

    def _get_or_create(self):
        LOG.debug('\tObtaining a pointer to container {}'.format(self.name))
        # Checking if container already exists
        self.container = self.get()
        # If doesn't exist creating a new container
        if not self.container:
            self.container = self._create()
        return
    
    def _update(self):
        self.container = self.get()
    
    def _create(self):
        return DOCKER.containers.create(
                self.image,
                command=self.command,
                environment=self.environment,
                volumes=self.volumes,
                network=self.default_network,
                privileged=True,
                name=self.name,
                detach=True,
                hostname=self.hostname,
                ports=self.ports,
                sysctls=self.sysctls,
                labels={PREFIX: self.name},
                tty=True
            )

    def get(self):
        try:
            return DOCKER.containers.get(self.name)
        except docker.errors.NotFound:
            return None

    def start(self):
        LOG.debug('\tStarting container {}'.format(self.name))

        if not self.container:
            self._get_or_create()
        if self.container.status == 'running':
            LOG.info('Container {} already running'.format(self.name))
            return 1
        if self.start_mode == 'auto':
            self._attach()
            self.container.start()
        elif self.start_mode == 'manual':
            self.container.start()
            self._update()
            self.pid = self.container.attrs.get('State', {}).get('Pid', None)
            LOG.debug('\tPID for container {} is {}'.format(self.name, self.pid))
            self.sandbox = self.container.attrs.get('NetworkSettings', {}).get('SandboxKey', None)
            LOG.debug('\tSandbox key for container {} is {}'.format(self.name, self.pid))
            self.container.pause()
            self._attach()
            self.container.unpause()
        else:
            LOG.info('Unsupported container start mode {}'.format(self.start_mode))
        return 0
    
    def add_ip(self, ip, intf):
        if self._verify_addr(ip):
            self.ips[intf] = ip

    def connect(self, interface, link):
        LOG.debug('\tCreating a pointer to network {} for interface {}'.format(link.name, interface))
        self.interfaces[interface] = link
        self._update_start_mode(interface, link)
        link.add_endpoint(self.name, interface, self.ips.get(interface, ""))
        return

    def _attach(self):
        for interface in sorted(self.interfaces):
            LOG.debug('\tAttaching container {} interface {} to its link'
                      .format(self.name, interface))
            link = self.interfaces[interface]
            link.connect(self, interface)
        return

    def kill(self):
        LOG.debug('\tKilling container {}'.format(self.name))
        if not self.container:
            self._get_or_create()
        
        if self.container.status not in ['running', 'paused']:
            LOG.info('Container {} is not running'.format(self.name))
            DOCKER.containers.prune(
                filters={'label': PREFIX}
            )
            return 1
        self.container.kill()
        return 0

    def publish_port(self, inside, outside):
        LOG.debug("Publishing {}'s port {} to {}".format(self.name, inside, outside))
        self.ports[inside] = outside
    
    def write_mem(self, filename):
        raise NotImplementedError("Save is only implemented for cEOS device type")

class Host(Device):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Overriding defaults
        self.type = 'host'
        self.image = "traffic-simulator-agent:latest"
        # CMD will contain a list of IPs to assign to interfaces
        self.command = []

    @staticmethod
    def _verify_addr(ip):
        prefix = netaddr.IPNetwork(ip)
        return prefix.prefixlen < 32

    def add_ip(self, ip, intf):
        LOG.debug("Adding IP address command line argument for IP {}".format(ip))
        if self._verify_addr(ip):
            self.command.append(':'.join([intf, ip]))

    def start(self, *args, **kwargs):
        super().start(*args, **kwargs)
        if self.start_mode == 'manual':
        # This is to fix the scripts who assign IPs in entrypoint
            LOG.debug("Running set_ips script")
            rc, output = self.container.exec_run("/set_ips.sh")
            LOG.debug("RC = {}, output = {}".format(rc, output))
        return 0

class CVP(Device):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Overriding defaults
        self.type = 'cvp'
        self.image = CVP_VERSION
        self.ports = {'443/tcp': 443}
        self.command = CVP_MEM_MAX
        self.entry_cmd = 'docker exec -it {} bash'.format(self.name)

    def publish_port(self, *args, **kwargs):
        raise NotImplementedError("Can't publish ports for {}".format(self.__class__))


class VEOS(Device):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Overriding defaults
        self.type = 'veos'
        self.image = 'veos:latest'
        self.entry_cmd = 'docker exec -it {} telnet localhost 23'.format(self.name)
        # Overriding extra variables
        self.volumes = self._get_config()

    def _get_config(self):
        
        startup = os.path.join(CONF_DIR, self.name)
        # Docker requires absolute path in volumes
        startup = os.path.abspath(startup)
        if os.path.isfile(startup):
            return {startup: {
                            'bind': "/mnt/flash/startup-config",
                            'mode': 'rw'}
                    }
        else:
            return {}


class VMX(Device):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Overriding defaults
        self.type = 'vmx'
        self.image = VMX_VERSION
        self.command = '--meshnet'
        self.entry_cmd = 'ssh vrnetlab@$(docker inspect {} --format \'{{.NetworkSettings.IPAddress}}\')'.format(self.name)

    def publish_port(self, *args, **kwargs):
        raise NotImplementedError("Can't publish ports for {}".format(self.__class__))

class CSR(Device):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Overriding defaults
        self.type = 'csr'
        self.image = CSR_VERSION
        self.command = '--meshnet'
        self.entry_cmd = 'ssh vrnetlab@$(docker inspect {} --format \'{{.NetworkSettings.IPAddress}}\')'.format(self.name)

    def publish_port(self, *args, **kwargs):
        raise NotImplementedError("Can't publish ports for {}".format(self.__class__))

class XRV(Device):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Overriding defaults
        self.type = 'xrv'
        self.image = XRV_VERSION
        self.command = '--meshnet'
        self.entry_cmd = 'ssh vrnetlab@$(docker inspect {} --format \'{{.NetworkSettings.IPAddress}}\')'.format(self.name)

    def publish_port(self, *args, **kwargs):
        raise NotImplementedError("Can't publish ports for {}".format(self.__class__))

class Generic(Device):
    def __init__(self, *args, **kwargs):
        image = kwargs.pop('image', "alpine")
        super().__init__(*args, **kwargs)
        # Overriding defaults
        self.image = image
        self.type = 'generic'
        self.entry_cmd = 'docker exec -it {} sh'.format(self.name)

    def publish_port(self, *args, **kwargs):
        raise NotImplementedError("Can't publish ports for {}".format(self.__class__))


class Veth(object):
    def __init__(self, name):
        self.name = name
        self.ipdb = IPDB()
        self.sideA = "{}-a".format(name)
        self.sideB = "{}-b".format(name)

    def _create(self):
        return self.ipdb.create(ifname=self.sideA, kind='veth', peer=self.sideB).commit()
         
    def _get(self, intf):
        return self.ipdb.interfaces.get(intf)

    def connect(self, device, interface):
        ns_fd = os.open('/proc/{}/ns/net'.format(device.pid), os.O_RDONLY) 
        veth = self._get(self.sideB)
        if not veth:
            self._create()
            veth = self._get(self.sideA)
        LOG.debug("Connecting {} to {}".format(veth.ifname, device.name))
        with veth as i:
            i.ifname = interface
            i.net_ns_fd = ns_fd
            i.up()
            # TODO Could add veth.add_ip('1.1.1.1/24') and get rid of set_ips.sh script
            LOG.debug("Create an interface for container {}: {}".format(device.name, i.review()))
        ns_name = device.sandbox.split('/')[-1]
        with NetNS(ns_name) as nl:
            with IPDB(nl=nl) as ns:
                with ns.interfaces[interface] as i:
                    if int(i.address[1], 16) & 0x02 > 0:
                        # MLAG mechanism uses the "locally administered bit" from the MAC address
                        # and practically, the Mlag agent will core when set.
                        LOG.debug(f"Changing interface mac from {i.address}")
                        i.set_address(i.address[0] + "0" + i.address[2:]).commit()
                        LOG.debug(f"to {i.address}")
                    i.up()
 

class Link(object):
    def __init__(self, link_type, name, link_driver=None, driver_opts=None):
        LOG.debug('\tConstructing a {} link with name {}'.format(link_type, name))
        self.name = '_'.join([PREFIX, name])
        self.link_type = link_type
        self.network = None
        self.opts = driver_opts if driver_opts else {}
        self.endpoints = list()

        if link_driver:
            self.driver = link_driver
        else:
            self.driver = 'bridge'
        LOG.debug('\t\tThe driver to be used for link {} is {}'.format(name, link_driver))

        if link_type == 'mpoint' and self.driver == 'veth':
            LOG.error('\t\tVeth driver is not supported with multipoint links')
        
        if not self.driver in SUPPORTED_DRIVERS:
            LOG.error("\t\tUnsupported link driver {}".format(link_driver))

        self.get_or_create()
    
    def add_endpoint(self, name, interface, ip):
        self.idx = len(self.endpoints)
        endpoint = (name, interface, ip, self.idx)
        self.endpoints.append(endpoint)

    def get_or_create(self):
        LOG.debug('\tObtaining a pointer to network {}'.format(self.name))
        self.network = self._get()
        if not self.network:
            self.network = self._create()
        return self.network

    def _create(self):
        LOG.debug('\tCreating a new network {} of type {}'.format(self.name, self.driver))
        return DOCKER.networks.create(
                self.name,
                driver=self.driver,
                labels={PREFIX: self.name},
                options=self.opts
            )

    def _get(self):
        LOG.debug("Trying to find an existing network with name {}".format(self.name))
        if self.driver == 'veth':
            return Veth(self.name)
        try:
            return DOCKER.networks.get(self.name)
        except docker.errors.NotFound:
            LOG.debug("Network {} not found".format(self.name))
            return None

    def connect(self, device, interface):
        LOG.debug('\tConnecting {}-type link to {}'.format(self.driver, device.name))
        if not self.network:
            self.get_or_create()
        if self.driver == 'veth':
            self.network.connect(device, interface)
        else:
            self.network.connect(device.container)
    
    def kill(self):
        if self.driver == 'veth':
            pass

def main():
    # Initializing main variables
    global PREFIX,  CONF_DIR, PUBLISH_BASE, OOB_PREFIX, VERSION, PUBLISH_SSH, \
        VMX_VERSION, CSR_VERSION, XRV_VERSION, CUSTOM_IMAGE, CVP_VERSION
    
    # Assigning arguments
    args    = parse_args()
    debug   = args.debug
    create  = args.create
    graph   = args.graph
    save    = args.save
    tar     = args.archive
    t_file  = os.path.join(os.getcwd(), args.topology)
    t_fn    = os.path.split(args.topology)[-1]
    t_file_pwd = os.path.join(os.getcwd(), os.path.split(args.topology)[0])
    destroy = args.destroy
    PREFIX  = t_fn.split('.')[0]

    # Logging settings
    if debug:
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO
    logging.basicConfig(level=log_level)

    # Loading topology YAML file
    with open(t_file, 'r') as stream:
        t_yml = yaml.safe_load(stream)
    LOG.debug("Loaded topology from YAML file {}\n {}"
                 .format(t_file, yaml.dump(t_yml)))
    if 'links' not in t_yml:
        LOG.info('"links" dictionary is not found in {}'
                    .format(t_file))
        return 1

    # Loading optional variables
    # First check if it's specified in topoology.yml
    # Then check if it's an env variable, last accept default
    CONF_DIR = t_yml.get('CONF_DIR',os.getenv('CONF_DIR', CONF_DIR))
    PUBLISH_BASE = t_yml.get('PUBLISH_BASE', os.getenv('PUBLISH_BASE', PUBLISH_BASE))
    OOB_PREFIX = t_yml.get('OOB_PREFIX',os.getenv('OOB_PREFIX', OOB_PREFIX))
    VERSION = int(t_yml.get('VERSION',os.getenv('VERSION', VERSION)))
    VMX_VERSION = t_yml.get('VMX_VERSION',os.getenv('VMX_VERSION', VMX_VERSION))
    CSR_VERSION = t_yml.get('CSR_VERSION',os.getenv('CSR_VERSION', CSR_VERSION))
    XRV_VERSION = t_yml.get('XRV_VERSION',os.getenv('XRV_VERSION', XRV_VERSION))
    CUSTOM_IMAGE = t_yml.get('CUSTOM_IMAGE',os.getenv('CUSTOM_IMAGE', CUSTOM_IMAGE))
    CVP_VERSION = t_yml.get('CVP_VERSION',os.getenv('CVP_VERSION', CVP_VERSION))
    PREFIX = t_yml.get('PREFIX',os.getenv('PREFIX', PREFIX))

    if VERSION == 2 and os.geteuid() != 0:
        LOG.info("Version 2 requires sudo. Restarting script with sudo")
        os.execvp("sudo", ["sudo"] + sys.argv)

    # Searching for CONF_DIR in the same dir as yaml file
    
    confdir_alt = os.path.join(t_file_pwd, CONF_DIR)
    if os.path.isdir(confdir_alt):
        CONF_DIR = confdir_alt
    LOG.debug("New CONFDIR is {}".format(CONF_DIR))

    # Versioned parsing logic
    if VERSION == 1:
        devices, links = parse_v1(t_yml)
    elif VERSION == 2:
        devices, links = parse_v2(t_yml)
    else:
        LOG.error("Specified version {} is not supported".format(VERSION))
        return 1


    
    LOG.debug("PUBLISH_BASE is {} of type {}".format(PUBLISH_BASE, type(PUBLISH_BASE)))
    # We're only publishing ceos ports, not host or cvp
    ceos_only = [k for k in devices.keys() if devices[k].type in ['ceos', 'veos']]
    # Publishing ports. If type is INT assuming default behaviour which is
    # Single mapping of inside 443 to outside PUBLISH_BASE+index
    if PUBLISH_BASE and (type(PUBLISH_BASE) == int):
        LOG.debug("Publish internal port 443 -> base {}".format(PUBLISH_BASE))
        base = int(PUBLISH_BASE)
        # Sort all device names alphabetically
        for idx,name in enumerate(sorted(ceos_only)):
            # Publish internal HTTPS port to external base
            devices[name].publish_port('443/tcp',base+idx)
    # The second case is when PUBLISH_BASE is a dict, in which case
    # Each element is a mapping: INTERNAL:EXTERNAL, where
    # INTERNAL is 'PORT/PROTO', e.g. '443/tcp' and
    # EXTERNAL is either an INTEGER, e.g. 8000 or None (random port)
    # Or a list with two elements [EXTERNAL_IP, EXTERNAL_PORT]
    # similar to http://docker-py.readthedocs.io/en/stable/containers.html
    # Example: PUBLISH_BASE: {443/tcp:[127.0.0.1,4343]}
    # Example: PUBLISH_BASE: {443/tcp:8000}
    # Example: PUBLISH_BASE: {443/tcp:None}
    elif type(PUBLISH_BASE) == dict:
        for inside,outside in PUBLISH_BASE.items():
            LOG.debug("Publish internal port {} -> {} (type {})".format(inside, outside, type(outside)))
            # Sort all device names alphabetically
            for idx,name in enumerate(sorted(ceos_only)):
                # If it's a list, increment the second element
                if type(outside) == list:
                    external = (outside[0], outside[1]+idx)
                # If it's an int, simply increment
                elif type(outside) == int:
                    external = outside + idx
                # Else it must be None
                else:
                    external = None
                devices[name].publish_port(inside,external)
        
    # Main logic
    if create:
        started = [device.start() == 0 for (name, device) in devices.items()]
        
        LOG.info(''.join(["\nalias {}='{}'".format(name, device.entry_cmd) for (name, device) in devices.items()]))

        if all(started):
            LOG.info('All devices started successfully')
            LOG.debug('Patching ConfigAgent to enable write mem')
            enable_write_mem(devices)
            LOG.debug('Enabling LLDP forwarding on Docker bridges')
            enable_lldp(links)
            LOG.debug('Enabling IP forwarding inside all containers')
            enable_ipForwarding(devices)
            LOG.debug('Enabling inter-bridge forwarding')
            enable_bridge_forwarding()
            create_d3_graph(devices, links)
            LOG.info("D3 graph created")
            import socket
            my_ip = socket.gethostbyname(socket.gethostname())
            LOG.info(f"URL: http://{my_ip}:8087")

        else:
            LOG.info('Devices have not been started')
            return 1
    elif destroy:
        killed = [device.kill() == 0 for (name, device) in devices.items()]
        # Regardless of whether we killed or not, try to prune unused objects
        DOCKER.networks.prune(
            filters={'label': PREFIX}
        )
        DOCKER.containers.prune(
            filters={'label': PREFIX}
        )
        LOG.info(''.join(['\nunalias {}'.format(name) for (name, device) in devices.items()]))
        kill_agetty()
        cleanup_bridge_forwarding()

        if all(killed):
            LOG.info('All devices destroyed successfully')  
        else:
            LOG.info('Devices have not been destroyed')
            return 1

        
    if save:
        # Hard-coding to CONF_DIR. Don't think that a flexible option is needed
        dirname = CONF_DIR
        if os.path.exists(dirname):
            ok = input('Config directory exists, existing files may be overwritten. Continue? [y/n]:')
        else:
            os.mkdir(dirname)
            ok = 'y'
        if ok == 'y':
            saved = [device.write_mem(os.path.join(dirname, device.name)) == 0 for (name, device) in devices.items() if device.type == 'ceos']
            if all(saved):
                LOG.info("All configs saved in {}".format(dirname))
            else:
                LOG.info("Some configs have not been saved")
                return 1
        else:
            LOG.info("Save interrupted")

    if tar:
        if not os.path.exists(CONF_DIR):
            os.mkdir(CONF_DIR)
        arch_fn = archive_topo(CONF_DIR, t_fn)
        LOG.info("Archive file {} created".format(arch_fn))

    return 0
        


if __name__ == '__main__':
    main()
