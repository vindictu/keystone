# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2012 OpenStack LLC
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
"""
To run these tests against a live database:
1. Modify the file `tests/backend_sql.conf` to use the connection for your
   live database
2. Set up a blank, live database.
3. run the tests using
    ./run_tests.sh -N  test_sql_upgrade
    WARNING::
        Your database will be wiped.
    Do not do this against a Database with valuable data as
    all data will be lost.
"""
import copy
import json
import uuid

from migrate.versioning import api as versioning_api
import sqlalchemy

from keystone.common import sql
from keystone.common.sql import migration
from keystone import config
from keystone import exception
from keystone import test

import default_fixtures


CONF = config.CONF
DEFAULT_DOMAIN_ID = CONF.identity.default_domain_id


class SqlUpgradeTests(test.TestCase):

    def initialize_sql(self):
        self.metadata = sqlalchemy.MetaData()
        self.metadata.bind = self.engine

    def setUp(self):
        super(SqlUpgradeTests, self).setUp()
        self.config([test.etcdir('keystone.conf.sample'),
                     test.testsdir('test_overrides.conf'),
                     test.testsdir('backend_sql.conf')])

        # create and share a single sqlalchemy engine for testing
        self.base = sql.Base()
        self.engine = self.base.get_engine(allow_global_engine=False)
        self.Session = self.base.get_sessionmaker(engine=self.engine,
                                                  autocommit=False)

        self.initialize_sql()
        self.repo_path = migration._find_migrate_repo()
        self.schema = versioning_api.ControlledSchema.create(
            self.engine,
            self.repo_path, 0)

        # auto-detect the highest available schema version in the migrate_repo
        self.max_version = self.schema.repository.version().version

    def tearDown(self):
        table = sqlalchemy.Table("migrate_version", self.metadata,
                                 autoload=True)
        self.downgrade(0)
        table.drop(self.engine, checkfirst=True)
        super(SqlUpgradeTests, self).tearDown()

    def test_blank_db_to_start(self):
        self.assertTableDoesNotExist('user')

    def test_start_version_0(self):
        version = migration.db_version()
        self.assertEqual(version, 0, "DB is at version 0")

    def test_two_steps_forward_one_step_back(self):
        """You should be able to cleanly undo and re-apply all upgrades.

        Upgrades are run in the following order::

            0 -> 1 -> 0 -> 1 -> 2 -> 1 -> 2 -> 3 -> 2 -> 3 ...
                 ^---------^    ^---------^    ^---------^

        """
        for x in range(1, self.max_version + 1):
            self.upgrade(x)
            self.downgrade(x - 1)
            self.upgrade(x)

    def assertTableColumns(self, table_name, expected_cols):
        """Asserts that the table contains the expected set of columns."""
        self.initialize_sql()
        table = self.select_table(table_name)
        actual_cols = [col.name for col in table.columns]
        self.assertEqual(expected_cols, actual_cols, '%s table' % table_name)

    def test_upgrade_0_to_1(self):
        self.upgrade(1)
        self.assertTableColumns("user", ["id", "name", "extra"])
        self.assertTableColumns("tenant", ["id", "name", "extra"])
        self.assertTableColumns("role", ["id", "name"])
        self.assertTableColumns("user_tenant_membership",
                                ["user_id", "tenant_id"])
        self.assertTableColumns("metadata", ["user_id", "tenant_id", "data"])
        self.populate_user_table()

    def test_upgrade_5_to_6(self):
        self.upgrade(5)
        self.assertTableDoesNotExist('policy')

        self.upgrade(6)
        self.assertTableExists('policy')
        self.assertTableColumns('policy', ['id', 'type', 'blob', 'extra'])

    def test_upgrade_8_to_10(self):
        self.upgrade(8)
        self.populate_user_table()
        self.populate_tenant_table()
        self.upgrade(10)
        self.assertTableColumns("user",
                                ["id", "name", "extra",
                                 "password", "enabled"])
        self.assertTableColumns("tenant",
                                ["id", "name", "extra", "description",
                                 "enabled"])
        self.assertTableColumns("role", ["id", "name", "extra"])
        self.assertTableColumns("user_tenant_membership",
                                ["user_id", "tenant_id"])
        self.assertTableColumns("metadata", ["user_id", "tenant_id", "data"])
        session = self.Session()
        user_table = sqlalchemy.Table("user",
                                      self.metadata,
                                      autoload=True)
        a_user = session.query(user_table).filter("id='foo'").one()
        self.assertTrue(a_user.enabled)
        a_user = session.query(user_table).filter("id='badguy'").one()
        self.assertFalse(a_user.enabled)
        tenant_table = sqlalchemy.Table("tenant",
                                        self.metadata,
                                        autoload=True)
        a_tenant = session.query(tenant_table).filter("id='baz'").one()
        self.assertEqual(a_tenant.description, 'description')
        session.commit()
        session.close()

    def test_downgrade_10_to_8(self):
        self.upgrade(10)
        self.populate_user_table(with_pass_enab=True)
        self.populate_tenant_table(with_desc_enab=True)
        self.downgrade(8)
        self.assertTableColumns('user',
                                ['id', 'name', 'extra'])
        self.assertTableColumns('tenant',
                                ['id', 'name', 'extra'])
        session = self.Session()
        user_table = sqlalchemy.Table("user",
                                      self.metadata,
                                      autoload=True)
        a_user = session.query(user_table).filter("id='badguy'").one()
        self.assertEqual(a_user.name, default_fixtures.USERS[2]['name'])
        tenant_table = sqlalchemy.Table("tenant",
                                        self.metadata,
                                        autoload=True)
        a_tenant = session.query(tenant_table).filter("id='baz'").one()
        self.assertEqual(a_tenant.name, default_fixtures.TENANTS[1]['name'])
        session.commit()
        session.close()

    def test_upgrade_10_to_13(self):
        self.upgrade(10)
        service_extra = {
            'name': uuid.uuid4().hex,
        }
        service = {
            'id': uuid.uuid4().hex,
            'type': uuid.uuid4().hex,
            'extra': json.dumps(service_extra),
        }
        endpoint_extra = {
            'publicurl': uuid.uuid4().hex,
            'internalurl': uuid.uuid4().hex,
            'adminurl': uuid.uuid4().hex,
        }
        endpoint = {
            'id': uuid.uuid4().hex,
            'region': uuid.uuid4().hex,
            'service_id': service['id'],
            'extra': json.dumps(endpoint_extra),
        }

        session = self.Session()
        self.insert_dict(session, 'service', service)
        self.insert_dict(session, 'endpoint', endpoint)
        session.commit()
        session.close()

        self.upgrade(13)
        self.assertTableColumns(
            'service',
            ['id', 'type', 'extra'])
        self.assertTableColumns(
            'endpoint',
            ['id', 'legacy_endpoint_id', 'interface', 'region', 'service_id',
             'url', 'extra'])

        endpoint_table = sqlalchemy.Table(
            'endpoint', self.metadata, autoload=True)

        session = self.Session()
        self.assertEqual(session.query(endpoint_table).count(), 3)
        for interface in ['public', 'internal', 'admin']:
            q = session.query(endpoint_table)
            q = q.filter_by(legacy_endpoint_id=endpoint['id'])
            q = q.filter_by(interface=interface)
            ref = q.one()
            self.assertNotEqual(ref.id, endpoint['id'])
            self.assertEqual(ref.legacy_endpoint_id, endpoint['id'])
            self.assertEqual(ref.interface, interface)
            self.assertEqual(ref.region, endpoint['region'])
            self.assertEqual(ref.service_id, endpoint['service_id'])
            self.assertEqual(ref.url, endpoint_extra['%surl' % interface])
            self.assertEqual(ref.extra, '{}')
        session.commit()
        session.close()

    def assertTenantTables(self):
        self.assertTableExists('tenant')
        self.assertTableExists('user_tenant_membership')
        self.assertTableDoesNotExist('project')
        self.assertTableDoesNotExist('user_project_membership')

    def assertProjectTables(self):
        self.assertTableExists('project')
        self.assertTableExists('user_project_membership')
        self.assertTableDoesNotExist('tenant')
        self.assertTableDoesNotExist('user_tenant_membership')

    def test_upgrade_tenant_to_project(self):
        self.upgrade(14)
        self.assertTenantTables()
        self.upgrade(15)
        self.assertProjectTables()

    def test_downgrade_project_to_tenant(self):
        # TODO(henry-nash): Debug why we need to re-load the tenant
        # or user_tenant_membership ahead of upgrading to project
        # in order for the assertProjectTables to work on sqlite
        # (MySQL is fine without it)
        self.upgrade(14)
        self.assertTenantTables()
        self.upgrade(15)
        self.assertProjectTables()
        self.downgrade(14)
        self.assertTenantTables()

    def test_upgrade_13_to_14(self):
        self.upgrade(13)
        self.upgrade(14)
        self.assertTableExists('group')
        self.assertTableExists('group_project_metadata')
        self.assertTableExists('group_domain_metadata')
        self.assertTableExists('user_group_membership')

    def test_upgrade_14_to_16(self):
        self.upgrade(14)
        self.populate_user_table(with_pass_enab=True)
        self.populate_tenant_table(with_desc_enab=True)
        self.upgrade(16)
        self.assertTableColumns("user",
                                ["id", "name", "extra",
                                 "password", "enabled", "domain_id"])
        session = self.Session()
        user_table = sqlalchemy.Table("user",
                                      self.metadata,
                                      autoload=True)
        a_user = session.query(user_table).filter("id='foo'").one()
        self.assertTrue(a_user.enabled)
        self.assertEqual(a_user.domain_id, DEFAULT_DOMAIN_ID)
        a_user = session.query(user_table).filter("id='badguy'").one()
        self.assertEqual(a_user.name, default_fixtures.USERS[2]['name'])
        self.assertEqual(a_user.domain_id, DEFAULT_DOMAIN_ID)
        project_table = sqlalchemy.Table("project",
                                         self.metadata,
                                         autoload=True)
        a_project = session.query(project_table).filter("id='baz'").one()
        self.assertEqual(a_project.description,
                         default_fixtures.TENANTS[1]['description'])
        self.assertEqual(a_project.domain_id, DEFAULT_DOMAIN_ID)
        session.commit()
        session.close()

    def test_downgrade_16_to_14(self):
        self.upgrade(16)
        self.populate_user_table(with_pass_enab_domain=True)
        self.populate_tenant_table(with_desc_enab_domain=True)
        self.downgrade(14)
        self.assertTableColumns("user",
                                ["id", "name", "extra",
                                 "password", "enabled"])
        session = self.Session()
        user_table = sqlalchemy.Table("user",
                                      self.metadata,
                                      autoload=True)
        a_user = session.query(user_table).filter("id='foo'").one()
        self.assertTrue(a_user.enabled)
        a_user = session.query(user_table).filter("id='badguy'").one()
        self.assertEqual(a_user.name, default_fixtures.USERS[2]['name'])
        tenant_table = sqlalchemy.Table("tenant",
                                        self.metadata,
                                        autoload=True)
        a_tenant = session.query(tenant_table).filter("id='baz'").one()
        self.assertEqual(a_tenant.description,
                         default_fixtures.TENANTS[1]['description'])
        session.commit()
        session.close()

    def test_downgrade_14_to_13(self):
        self.upgrade(14)
        self.downgrade(13)
        self.assertTableDoesNotExist('group')
        self.assertTableDoesNotExist('group_project_metadata')
        self.assertTableDoesNotExist('group_domain_metadata')
        self.assertTableDoesNotExist('user_group_membership')

    def test_downgrade_13_to_10(self):
        self.upgrade(13)

        service_extra = {
            'name': uuid.uuid4().hex,
        }
        service = {
            'id': uuid.uuid4().hex,
            'type': uuid.uuid4().hex,
            'extra': json.dumps(service_extra),
        }

        common_endpoint_attrs = {
            'legacy_endpoint_id': uuid.uuid4().hex,
            'region': uuid.uuid4().hex,
            'service_id': service['id'],
            'extra': json.dumps({}),
        }
        endpoints = {
            'public': {
                'id': uuid.uuid4().hex,
                'interface': 'public',
                'url': uuid.uuid4().hex,
            },
            'internal': {
                'id': uuid.uuid4().hex,
                'interface': 'internal',
                'url': uuid.uuid4().hex,
            },
            'admin': {
                'id': uuid.uuid4().hex,
                'interface': 'admin',
                'url': uuid.uuid4().hex,
            },
        }

        session = self.Session()
        self.insert_dict(session, 'service', service)
        for endpoint in endpoints.values():
            endpoint.update(common_endpoint_attrs)
            self.insert_dict(session, 'endpoint', endpoint)
        session.commit()
        session.close()

        self.downgrade(9)

        self.assertTableColumns(
            'service',
            ['id', 'type', 'extra'])
        self.assertTableColumns(
            'endpoint',
            ['id', 'region', 'service_id', 'extra'])

        endpoint_table = sqlalchemy.Table(
            'endpoint', self.metadata, autoload=True)

        session = self.Session()
        self.assertEqual(session.query(endpoint_table).count(), 1)
        q = session.query(endpoint_table)
        q = q.filter_by(id=common_endpoint_attrs['legacy_endpoint_id'])
        ref = q.one()
        self.assertEqual(ref.id, common_endpoint_attrs['legacy_endpoint_id'])
        self.assertEqual(ref.region, endpoint['region'])
        self.assertEqual(ref.service_id, endpoint['service_id'])
        extra = json.loads(ref.extra)
        for interface in ['public', 'internal', 'admin']:
            expected_url = endpoints[interface]['url']
            self.assertEqual(extra['%surl' % interface], expected_url)
        session.commit()
        session.close()

    def insert_dict(self, session, table_name, d):
        """Naively inserts key-value pairs into a table, given a dictionary."""
        this_table = sqlalchemy.Table(table_name, self.metadata, autoload=True)
        insert = this_table.insert()
        insert.execute(d)
        session.commit()

    def test_downgrade_to_0(self):
        self.upgrade(self.max_version)
        self.downgrade(0)
        for table_name in ["user", "token", "role", "user_tenant_membership",
                           "metadata"]:
            self.assertTableDoesNotExist(table_name)

    def test_upgrade_6_to_7(self):
        self.upgrade(6)
        self.assertTableDoesNotExist('credential')
        self.assertTableDoesNotExist('domain')
        self.assertTableDoesNotExist('user_domain_metadata')

        self.upgrade(7)
        self.assertTableExists('credential')
        self.assertTableColumns('credential', ['id', 'user_id', 'project_id',
                                               'blob', 'type', 'extra'])
        self.assertTableExists('domain')
        self.assertTableColumns('domain', ['id', 'name', 'enabled', 'extra'])
        self.assertTableExists('user_domain_metadata')
        self.assertTableColumns('user_domain_metadata',
                                ['user_id', 'domain_id', 'data'])

    def populate_user_table(self, with_pass_enab=False,
                            with_pass_enab_domain=False):
        # Populate the appropriate fields in the user
        # table, depending on the parameters:
        #
        # Default: id, name, extra
        # pass_enab: Add password, enabled as well
        # pass_enab_domain: Add password, enabled and domain as well
        #
        this_table = sqlalchemy.Table("user",
                                      self.metadata,
                                      autoload=True)
        for user in default_fixtures.USERS:
            extra = copy.deepcopy(user)
            extra.pop('id')
            extra.pop('name')

            if with_pass_enab:
                password = extra.pop('password', None)
                enabled = extra.pop('enabled', True)
                ins = this_table.insert().values(
                    {'id': user['id'],
                     'name': user['name'],
                     'password': password,
                     'enabled': bool(enabled),
                     'extra': json.dumps(extra)})
            else:
                if with_pass_enab_domain:
                    password = extra.pop('password', None)
                    enabled = extra.pop('enabled', True)
                    extra.pop('domain_id')
                    ins = this_table.insert().values(
                        {'id': user['id'],
                         'name': user['name'],
                         'domain_id': user['domain_id'],
                         'password': password,
                         'enabled': bool(enabled),
                         'extra': json.dumps(extra)})
                else:
                    ins = this_table.insert().values(
                        {'id': user['id'],
                         'name': user['name'],
                         'extra': json.dumps(extra)})
            self.engine.execute(ins)

    def populate_tenant_table(self, with_desc_enab=False,
                              with_desc_enab_domain=False):
        # Populate the appropriate fields in the tenant or
        # project table, depending on the parameters
        #
        # Default: id, name, extra
        # desc_enab: Add description, enabled as well
        # desc_enab_domain: Add description, enabled and domain as well,
        #                   plus use project instead of tenant
        #
        if with_desc_enab_domain:
            # By this time tenants are now projects
            this_table = sqlalchemy.Table("project",
                                          self.metadata,
                                          autoload=True)
        else:
            this_table = sqlalchemy.Table("tenant",
                                          self.metadata,
                                          autoload=True)

        for tenant in default_fixtures.TENANTS:
            extra = copy.deepcopy(tenant)
            extra.pop('id')
            extra.pop('name')

            if with_desc_enab:
                desc = extra.pop('description', None)
                enabled = extra.pop('enabled', True)
                ins = this_table.insert().values(
                    {'id': tenant['id'],
                     'name': tenant['name'],
                     'description': desc,
                     'enabled': bool(enabled),
                     'extra': json.dumps(extra)})
            else:
                if with_desc_enab_domain:
                    desc = extra.pop('description', None)
                    enabled = extra.pop('enabled', True)
                    extra.pop('domain_id')
                    ins = this_table.insert().values(
                        {'id': tenant['id'],
                         'name': tenant['name'],
                         'domain_id': tenant['domain_id'],
                         'description': desc,
                         'enabled': bool(enabled),
                         'extra': json.dumps(extra)})
                else:
                    ins = this_table.insert().values(
                        {'id': tenant['id'],
                         'name': tenant['name'],
                         'extra': json.dumps(extra)})
            self.engine.execute(ins)

    def select_table(self, name):
        table = sqlalchemy.Table(name,
                                 self.metadata,
                                 autoload=True)
        s = sqlalchemy.select([table])
        return s

    def assertTableExists(self, table_name):
        try:
            self.select_table(table_name)
        except sqlalchemy.exc.NoSuchTableError:
            raise AssertionError('Table "%s" does not exist' % table_name)

    def assertTableDoesNotExist(self, table_name):
        """Asserts that a given table exists cannot be selected by name."""
        # Switch to a different metadata otherwise you might still
        # detect renamed or dropped tables
        try:
            temp_metadata = sqlalchemy.MetaData()
            temp_metadata.bind = self.engine
            table = sqlalchemy.Table(table_name,
                                     temp_metadata,
                                     autoload=True)
        except sqlalchemy.exc.NoSuchTableError:
            pass
        else:
            raise AssertionError('Table "%s" already exists' % table_name)

    def upgrade(self, *args, **kwargs):
        self._migrate(*args, **kwargs)

    def downgrade(self, *args, **kwargs):
        self._migrate(*args, downgrade=True, **kwargs)

    def _migrate(self, version, repository=None, downgrade=False):
        repository = repository or self.repo_path
        err = ''
        version = versioning_api._migrate_version(self.schema,
                                                  version,
                                                  not downgrade,
                                                  err)
        changeset = self.schema.changeset(version)
        for ver, change in changeset:
            self.schema.runchange(ver, change, changeset.step)
        self.assertEqual(self.schema.version, version)
