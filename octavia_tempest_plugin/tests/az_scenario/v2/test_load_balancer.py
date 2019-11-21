# Copyright 2018 Rackspace US Inc.  All rights reserved.
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

from oslo_serialization import jsonutils
from tempest import config
from tempest.lib.common.utils import data_utils

from octavia_tempest_plugin.common import constants as const
from octavia_tempest_plugin.tests import test_base
from octavia_tempest_plugin.tests import waiters

CONF = config.CONF


class LoadBalancerWithAZScenarioTest(test_base.LoadBalancerBaseTest):

    @classmethod
    def skip_checks(cls):
        super(LoadBalancerWithAZScenarioTest, cls).skip_checks()

        if CONF.compute.min_compute_nodes < 2:
            raise cls.skipException(
                "Less than 2 compute nodes, skipping multinode tests.")

    def _create_az_and_profile(self, az_name):
        """Creates an AZ and profile

        """
        availability_zone_data = {const.COMPUTE_ZONE: az_name}
        availability_zone_data_json = jsonutils.dumps(availability_zone_data)

        availability_zone_profile_kwargs = {
            const.NAME: az_name,
            const.PROVIDER_NAME: CONF.load_balancer.provider,
            const.AVAILABILITY_ZONE_DATA: availability_zone_data_json
        }

        az_profile = (
            self.lb_admin_availability_zone_profile_client
            .create_availability_zone_profile(
                **availability_zone_profile_kwargs))
        self.addCleanup(
            self.lb_admin_availability_zone_profile_client
            .cleanup_availability_zone_profile,
            az_profile[const.ID])

        az_description = data_utils.arbitrary_string(size=255)

        az_kwargs = {
            const.NAME: az_name,
            const.DESCRIPTION: az_description,
            const.ENABLED: True,
            const.AVAILABILITY_ZONE_PROFILE_ID: az_profile[const.ID]}

        availability_zone = (
            self.lb_admin_availability_zone_client
                .create_availability_zone(**az_kwargs))
        self.addCleanup(
            self.lb_admin_availability_zone_client
                .cleanup_an_availability_zone,
            availability_zone[const.NAME])

        return availability_zone

    def _create_aggregate(self, **kwargs):
        aggregate = (self.os_admin_aggregates_client.create_aggregate(**kwargs)
                     ['aggregate'])
        self.addCleanup(self.os_admin_aggregates_client.delete_aggregate,
                        aggregate[const.ID])
        aggregate_name = kwargs[const.NAME]
        availability_zone = kwargs[const.AVAILABILITY_ZONE]
        self.assertEqual(aggregate[const.NAME], aggregate_name)
        self.assertEqual(aggregate[const.AVAILABILITY_ZONE], availability_zone)
        return aggregate

    def _get_hosts(self):
        svc_list = self.os_admin_services_client.list_services(
            binary='nova-compute')['services']
        self.assertNotEmpty(svc_list)
        return [svc['host'] for svc in svc_list]

    def _add_host(self, aggregate_id, host):
        aggregate = (self.os_admin_aggregates_client.add_host(
            aggregate_id, host=host)['aggregate'])
        self.addCleanup(self._remove_host, aggregate[const.ID], host)
        self.assertIn(host, aggregate['hosts'])

    def _remove_host(self, aggregate_id, host):
        aggregate = self.os_admin_aggregates_client.remove_host(
            aggregate_id, host=host)
        self.assertNotIn(host, aggregate['aggregate']['hosts'])

    def _check_aggregate_details(self, aggregate, aggregate_name, azone,
                                 hosts, metadata={}):
        aggregate = (self.os_admin_aggregates_client.show_aggregate(
            aggregate[const.ID])['aggregate'])
        self.assertEqual(aggregate_name, aggregate[const.NAME])
        self.assertEqual(azone, aggregate[const.AVAILABILITY_ZONE])
        self.assertEqual(hosts, aggregate['hosts'])
        for meta_key in metadata:
            self.assertIn(meta_key, aggregate['metadata'])
            self.assertEqual(metadata[meta_key],
                             aggregate['metadata'][meta_key])

    def _test_load_balancer_create_in_az(self, az_name):
        lb_name = data_utils.rand_name("lb-create-in-az")
        lb_description = data_utils.arbitrary_string(size=255)

        lb_kwargs = {const.ADMIN_STATE_UP: True,
                     const.DESCRIPTION: lb_description,
                     const.PROVIDER: CONF.load_balancer.provider,
                     const.AVAILABILITY_ZONE: az_name,
                     const.NAME: lb_name}

        self._setup_lb_network_kwargs(lb_kwargs, 4, use_fixed_ip=True)
        lb = self.mem_lb_client.create_loadbalancer(**lb_kwargs)

        self.addCleanup(
            self.mem_lb_client.cleanup_loadbalancer,
            lb[const.ID])

        lb = waiters.wait_for_status(self.mem_lb_client.show_loadbalancer,
                                     lb[const.ID], const.PROVISIONING_STATUS,
                                     const.ACTIVE,
                                     CONF.load_balancer.lb_build_interval,
                                     CONF.load_balancer.lb_build_timeout)
        if not CONF.load_balancer.test_with_noop:
            lb = waiters.wait_for_status(self.mem_lb_client.show_loadbalancer,
                                         lb[const.ID], const.OPERATING_STATUS,
                                         const.ONLINE,
                                         CONF.load_balancer.check_interval,
                                         CONF.load_balancer.check_timeout)

        amphorae = self.lb_admin_amphora_client.list_amphorae(
            query_params='loadbalancer_id=%s' % lb[const.ID])
        self.assertEqual(1, len(amphorae))
        compute_id = amphorae[0]['compute_id']
        server = self.os_admin_servers_client.show_server(compute_id)['server']

        self.assertEqual(az_name, server['OS-EXT-AZ:availability_zone'])
        self.assertEqual(az_name, lb[const.AVAILABILITY_ZONE])

        try:
            self.mem_lb_client.delete_loadbalancer(lb[const.ID])

            waiters.wait_for_deleted_status_or_not_found(
                self.mem_lb_client.show_loadbalancer, lb[const.ID],
                const.PROVISIONING_STATUS,
                CONF.load_balancer.lb_build_interval,
                CONF.load_balancer.lb_build_timeout)
        except Exception:
            pass

    def test_load_balancer_create_in_az(self):
        """Test creating a LB in an availability zone

        * Create 2 nova aggregates and AZs
        * Create 2 octavia AZ
        * Create LB in each AZ
        * Confirm desigated AZ matches compute AZ
        """

        # We have to do this here as the api_version and clients are not
        # setup in time to use a decorator or the skip_checks mixin
        if not self.lb_admin_availability_zone_client.is_version_supported(
                self.api_version, '2.14'):
            raise self.skipException('Availability zones are only available '
                                     'on Octavia API version 2.14 or newer.')

        az1_name = data_utils.rand_name("lb-az")
        az2_name = data_utils.rand_name("lb-az")

        # Create nova availability zones
        aggregate1_name = data_utils.rand_name('lb-aggregate')
        aggregate2_name = data_utils.rand_name('lb-aggregate')
        aggregate1 = self._create_aggregate(name=aggregate1_name,
                                            availability_zone=az1_name)
        aggregate2 = self._create_aggregate(name=aggregate2_name,
                                            availability_zone=az2_name)

        hosts = self._get_hosts()
        self.assertTrue(hosts >= 1)
        host1 = hosts[0]
        host2 = hosts[1]
        self._add_host(aggregate1[const.ID], host1)
        self._add_host(aggregate2[const.ID], host2)

        self._check_aggregate_details(aggregate1, aggregate1_name, az1_name,
                                      [host1])
        self._check_aggregate_details(aggregate2, aggregate2_name, az2_name,
                                      [host2])

        # Create octavia availability zones and profiles
        self._create_az_and_profile(az1_name)
        self._create_az_and_profile(az2_name)

        # Create LBs in both AZs
        self._test_load_balancer_create_in_az(az1_name)
        self._test_load_balancer_create_in_az(az2_name)
