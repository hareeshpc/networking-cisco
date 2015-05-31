# Copyright 2015 Cisco Systems, Inc.  All rights reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import logging
from lxml import etree
import netaddr
from xml.dom import minidom
import xml.etree.ElementTree as ET

from neutron.i18n import _LE, _LI, _LW
from networking_cisco.plugins.cisco.cfg_agent import cfg_exceptions as cfg_exc
from networking_cisco.plugins.cisco.cfg_agent.device_drivers.csr1kv import (
    csr1kv_routing_driver as driver)
from networking_cisco.plugins.cisco.cfg_agent.device_drivers.csr1kv import (
    cisco_csr1kv_snippets as snippets)

LOG = logging.getLogger(__name__)


class CSR1kvHotPlugRoutingDriver(driver.CSR1kvRoutingDriver):
    """CSR1kv Hotplugging Routing Driver."""

    def __init__(self, **device_params):
        super(CSR1kvHotPlugRoutingDriver, self).__init__(**device_params)

    def internal_network_added(self, ri, port):
        self._csr_configure_interface(ri, port)

    def internal_network_removed(self, ri, port):
        self._csr_deconfigure_interface(port)

    def external_gateway_added(self, ri, ex_gw_port):
        self._csr_configure_interface(ri, ex_gw_port)
        ex_gw_ip = ex_gw_port['subnets'][0]['gateway_ip']
        if ex_gw_ip:
            # Set default route via this network's gateway ip
            self._csr_add_default_route(ri, ex_gw_ip)

    def external_gateway_removed(self, ri, ex_gw_port):
        ex_gw_ip = ex_gw_port['subnet'][0]['gateway_ip']
        if ex_gw_ip:
            self._csr_remove_default_route(ri, ex_gw_ip)
        self._csr_deconfigure_interface(ex_gw_port)

    def _enable_intfs(self, conn):
        return True

    def _csr_configure_interface(self, ri, port):
        vrf_name = self._csr_get_vrf_name(ri)
        ip_cidr = port['ip_cidr']
        netmask = netaddr.IPNetwork(ip_cidr).netmask
        gateway_ip = ip_cidr.split('/')[0]
        interface_name = self._get_interface_name_from_hosting_port(port)
        if not interface_name:
            params = {'id': port['id'], 'mac': port['mac_address']}
            raise cfg_exc.CSR1kvMissingInterfaceException(**params)
        self._configure_interface(interface_name, vrf_name,
                                  gateway_ip, netmask)

    def _configure_interface(self, if_name, vrf_name, ip, netmask):
        confstr = snippets.CONFIGURE_INTERFACE % (if_name, vrf_name,
                                                  ip, netmask)
        self._edit_running_config(confstr, 'CONFIGURE_INTERFACE')

    def _csr_deconfigure_interface(self, port):
        if_name = self._get_interface_name_from_hosting_port(port)
        if if_name and self._interface_exists(if_name):
            self._deconfigure_interface(if_name)
        else:
            LOG.debug("Interface %s not present. Not deconfiguring"), if_name

    def _deconfigure_interface(self, if_name):
        confstr = snippets.DECONFIGURE_INTERFACE % if_name
        self._edit_running_config(confstr, 'DECONFIGURE_INTERFACE')

    def _get_interface_name_from_hosting_port(self, port):
        mac = netaddr.EUI(port['mac_address'])
        mac_interface_dict = self._get_VNIC_mapping()
        if mac in mac_interface_dict:
            interface_name = mac_interface_dict[mac]
            LOG.info(_LI("Interface name for hosting port with mac:%(mac)s "
                         "is %(name)s"), {'mac': mac, 'name': interface_name})
            return interface_name

    def _get_VNIC_mapping(self):
        """ Returns a dict of mac addresses(EUI format) and interface names"""
        conn = self._get_connection()
        rpc_obj = conn.get(filter=snippets.GET_VNIC_MAPPING)
        raw_xml = etree.fromstring(rpc_obj.xml)
        formatted_xml = self._prettify(raw_xml)
        root = etree.fromstring(formatted_xml)
        # ToDo: Finalize correct namespace used. Differs among CSR builds.
        # Either namespaces={'ns0': 'urn:ietf:params:netconf:base:1.0'}) OR
        # namespaces={'ns0': 'urn:ietf:params:xml:ns:netconf:base:1.0'})
        subelements = root.xpath(
            '/ns0:rpc-reply/ns0:data/ns0:cli-oper-data-block/ns0:item/'
            'ns0:response',
            namespaces={'ns0': 'urn:ietf:params:xml:ns:netconf:base:1.0'})
        raw_value = subelements[0].text
        raw_list = raw_value.rstrip().split('\n')
        response_dict = {}
        for i in raw_list:
            if 'GigabitEthernet' in i:# We got a vnic mapping line
                tags = i.split()
                response_dict[netaddr.EUI(tags[2])] = tags[0]
        return response_dict

    def _prettify(self, elem):
        """Return a namespace added and prettified XML string for element."""
        rough_string = ET.tostring(elem, 'utf-8')
        reparsed = minidom.parseString(rough_string)
        res = reparsed.toprettyxml(indent="\t")
        return res