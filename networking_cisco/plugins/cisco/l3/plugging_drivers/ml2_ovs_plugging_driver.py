import eventlet

from oslo_log import log as logging
from sqlalchemy.sql import expression as expr


from neutron.api.v2 import attributes
from neutron.common import exceptions as n_exc
import networking_cisco.plugins.cisco.common.cisco_exceptions as c_exc
from neutron.db import models_v2
from neutron.i18n import _LE, _LI, _LW
from neutron import manager
from networking_cisco.plugins.cisco.db.l3.device_handling_db import (
    DeviceHandlingMixin)
import networking_cisco.plugins.cisco.l3.plugging_drivers as plug
from neutron.plugins.common import constants as svc_constants

LOG = logging.getLogger(__name__)

DELETION_ATTEMPTS = 4

import time
from functools import wraps

class ML2OVSPluggingDriver(plug.PluginSidePluggingDriver):
    """Driver class for service VMs used with the ML2 OVS plugin.

    The driver makes use of ML2 L2 API.
    """

    def retry(ExceptionToCheck, tries=4, delay=3, backoff=2):
        """Retry calling the decorated function using an exponential backoff.

        Reference: http://www.saltycrane.com/blog/2009/11/trying-out-retry
        -decorator-python/

        :param ExceptionToCheck: the exception to check. may be a tuple of
            exceptions to check
        :param tries: number of times to try (not retry) before giving up
        :param delay: initial delay between retries in seconds
        :param backoff: backoff multiplier e.g. value of 2 will double the delay
            each retry
        """

        def deco_retry(f):
            @wraps(f)
            def f_retry(*args, **kwargs):
                mtries, mdelay = tries, delay
                while mtries > 1:
                    try:
                        return f(*args, **kwargs)
                    except ExceptionToCheck, e:
                        LOG.error(_("%(ex)s, Retrying in %(delay)d seconds.."),
                                  {'ex': str(e), 'delay': mdelay})
                        time.sleep(mdelay)
                        mtries -= 1
                        mdelay *= backoff
                return f(*args, **kwargs)

            return f_retry  # true decorator

        return deco_retry

    @property
    def _core_plugin(self):
        return manager.NeutronManager.get_plugin()

    @property
    def svc_vm_mgr(self):
        return manager.NeutronManager.get_service_plugins().get(
            svc_constants.L3_ROUTER_NAT)._svc_vm_mgr

    def create_hosting_device_resources(self, context, complementary_id,
                                        tenant_id, mgmt_nw_id,
                                        mgmt_sec_grp_id, max_hosted):
        """Create resources for a hosting device in a plugin specific way."""
        mgmt_port = None
        if mgmt_nw_id is not None and tenant_id is not None:
            # Create port for mgmt interface
            p_spec = {'port': {
                'tenant_id': tenant_id,
                'admin_state_up': True,
                'name': 'mgmt',
                'network_id': mgmt_nw_id,
                'mac_address': attributes.ATTR_NOT_SPECIFIED,
                'fixed_ips': attributes.ATTR_NOT_SPECIFIED,
                'device_id': "",
                # Use device_owner attribute to ensure we can query for these
                # ports even before Nova has set device_id attribute.
                'device_owner': complementary_id}}
            try:
                mgmt_port = self._core_plugin.create_port(context, p_spec)
            except n_exc.NeutronException as e:
                LOG.error(_('Error %s when creating management port. '
                            'Cleaning up.'), e)
                self.delete_hosting_device_resources(
                    context, tenant_id, mgmt_port)
                mgmt_port = None
        # We are setting the 'ports' to an empty list as it is expected by
        # the callee: device_handling_db._create_csr1kv_vm_hosting_device()
        return {'mgmt_port': mgmt_port, 'ports': []}

    def get_hosting_device_resources(self, context, id, complementary_id,
                                     tenant_id, mgmt_nw_id):
        """Returns information about all resources for a hosting device."""
        ports, nets, subnets = [], [], []
        mgmt_port = None
        # Ports for hosting device may not yet have 'device_id' set to
        # Nova assigned uuid of VM instance. However, those ports will still
        # have 'device_owner' attribute set to complementary_id. Hence, we
        # use both attributes in the query to ensure we find all ports.
        query = context.session.query(models_v2.Port)
        query = query.filter(expr.or_(
            models_v2.Port.device_id == id,
            models_v2.Port.device_owner == complementary_id))
        for port in query:
            if port['network_id'] != mgmt_nw_id:
                raise Exception
            else:
                mgmt_port = port
        return {'mgmt_port': mgmt_port}

    def delete_hosting_device_resources(self, context, tenant_id, mgmt_port,
                                        **kwargs):
        """Deletes resources for a hosting device in a plugin specific way."""

        if mgmt_port is not None:
            try:
                self._delete_resource_port(context, mgmt_port['id'])
            except n_exc.NeutronException, e:
                LOG.error(_("Unable to delete port:%(port)s after %(tries)d"
                            " attempts due to exception %(exception)s. "
                            "Skipping it"), {'port': mgmt_port['id'],
                                             'tries': DELETION_ATTEMPTS,
                                             'exception': str(e)})

    @retry(n_exc.NeutronException, DELETION_ATTEMPTS, 1)
    def _delete_resource_port(self, context, port_id):
        try:
            self._core_plugin.delete_port(context, port_id)
            LOG.info(_("Port %s deleted successfully"), port_id)
        except n_exc.PortNotFound:
            LOG.warning(_('Trying to delete port:%s, but port not found'),
                        port_id)

    @retry(c_exc.PortNotUnBoundException, tries=6)
    def _is_port_unbound(self, context, port_db):
        # Nova will unbind the port asynchronously after the unplug. So we need
        # to expire the hosting port to fetch the updated info.
        context.session.expire(port_db.hosting_info.hosting_port)
        port_db = self._core_plugin._get_port(context, port_db.id)
        hport = port_db.hosting_info.hosting_port
        if hport['device_id'] == '' and hport['device_owner'] == '':
            LOG.debug("Hosting port has been unbound. Proceed to delete it")
            return True
        else:
            raise c_exc.PortNotUnBoundException(port_id=hport.id)

    def setup_logical_port_connectivity(self, context, port_db,
                                        hosting_device_id):
        """Establishes connectivity for a logical port.

        This is done by hot plugging the interface(VIF) corresponding to the
        port from the CSR."""

        hosting_port = port_db.hosting_info.hosting_port
        if hosting_port:
            try:
                self.svc_vm_mgr.interface_attach(
                    hosting_device_id, hosting_port.id)
                LOG.debug("Setup logical port completed for port:%s",
                          hosting_port.id)
            except Exception as e:
                LOG.error(_LE("Failed to attach interface mapped to port:"
                              "%(p_id)s on hosting device:%(hd_id)s due to "
                              "error %(error)s"), {'p_id': hosting_port.id,
                                                   'hd_id': hosting_device_id,
                                                   'error': str(e)})

    def teardown_logical_port_connectivity(self, context, port_db,
                                           hosting_device_id):
        """Removes connectivity for a logical port.

        This is done by hot unplugging the interface(VIF) corresponding to the
        port from the CSR.
        """
        if port_db is None or port_db.get('id') is None:
            LOG.warning(_LW("Port id is None! Cannot remove port "
                            "from hosting_device:%s"), hosting_device_id)
            return
        hosting_port = port_db.hosting_info.hosting_port
        try:
            self.svc_vm_mgr.interface_detach(hosting_device_id,
                                             hosting_port.id)
            if self._is_port_unbound(context, port_db):
                self._delete_resource_port(context, hosting_port.id)
                LOG.debug("Done teardown logical port connectivity for port:%s",
                           port_db['id'])
        except Exception as e:
            LOG.error(_LE("Failed to detach interface corresponding to port:"
                          "%(p_id)s on hosting device:%(hd_id)s due to "
                          "error %(error)s"), {'p_id': hosting_port.id,
                                               'hd_id': hosting_device_id,
                                               'error': str(e)})

    def extend_hosting_port_info(self, context, port_db, hosting_info):
        """Extends hosting information for a logical port."""
        return

    def allocate_hosting_port(self, context, router_id, port_db, network_type,
                              hosting_device_id):
        """Allocates a hosting port for a logical port.

        We create a hosting port for the router port
        """
        l3admin_tenant_id = DeviceHandlingMixin.l3_tenant_id()
        hostingport_name = 'hostingport_' + port_db['id'][:8]
        p_spec = {'port': {
                  'tenant_id': l3admin_tenant_id,
                  'admin_state_up': True,
                  'name': hostingport_name,
                  'network_id': port_db['network_id'],
                  'mac_address': attributes.ATTR_NOT_SPECIFIED,
                  'fixed_ips': [],
                  'device_id': '',
                  'device_owner': ''}}
        try:
            hosting_port = self._core_plugin.create_port(context, p_spec)
        except n_exc.NeutronException as e:
            LOG.error(_('Error %s when creating hosting port'
                        'Cleaning up.'), e)
            self.delete_hosting_device_resources(
                context, l3admin_tenant_id, hosting_port)
            hosting_port = None
        finally:
            if hosting_port:
                return {'allocated_port_id': hosting_port['id'],
                        'allocated_vlan': None}
            else:
                return None
