# Copyright 2015 Red Hat, Inc.
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

"""Support for introspection rules."""

import jsonpath_rw as jsonpath
import jsonschema
from oslo_db import exception as db_exc
from oslo_utils import timeutils
from oslo_utils import uuidutils
import six
from sqlalchemy import orm

from ironic_inspector.common.i18n import _
from ironic_inspector import db
from ironic_inspector.plugins import base as plugins_base
from ironic_inspector import utils


LOG = utils.getProcessingLogger(__name__)
_CONDITIONS_SCHEMA = None
_ACTIONS_SCHEMA = None


def conditions_schema():
    global _CONDITIONS_SCHEMA
    if _CONDITIONS_SCHEMA is None:
        condition_plugins = [x.name for x in
                             plugins_base.rule_conditions_manager()]
        _CONDITIONS_SCHEMA = {
            "title": "Inspector rule conditions schema",
            "type": "array",
            # we can have rules that always apply
            "minItems": 0,
            "items": {
                "type": "object",
                # field might become optional in the future, but not right now
                "required": ["op", "field"],
                "properties": {
                    "op": {
                        "description": "condition operator",
                        "enum": condition_plugins
                    },
                    "field": {
                        "description": "JSON path to field for matching",
                        "type": "string"
                    },
                    "multiple": {
                        "description": "how to treat multiple values",
                        "enum": ["all", "any", "first"]
                    },
                    "invert": {
                        "description": "whether to invert the result",
                        "type": "boolean"
                    },
                },
                # other properties are validated by plugins
                "additionalProperties": True
            }
        }

    return _CONDITIONS_SCHEMA


def actions_schema():
    global _ACTIONS_SCHEMA
    if _ACTIONS_SCHEMA is None:
        action_plugins = [x.name for x in
                          plugins_base.rule_actions_manager()]
        _ACTIONS_SCHEMA = {
            "title": "Inspector rule actions schema",
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["action"],
                "properties": {
                    "action": {
                        "description": "action to take",
                        "enum": action_plugins
                    },
                },
                # other properties are validated by plugins
                "additionalProperties": True
            }
        }

    return _ACTIONS_SCHEMA


class SourceData(object):
    _DEFAULT_SCHEME = 'data'
    _URI_SEPARATOR = '://'

    def __init__(self, node_info, intr_data, uri):
        self._scheme_data_sources = {
            'data': [intr_data],
            'node': [node_info.node().to_dict()]
            'port': [p.to_dict() for p in node_info.ports()]
        }
        scheme, path = SourceData._parse_uri(uri)
        try:
            self._data = self._scheme_data_sources[scheme]
        except KeyError:
            raise utils.Error(_('Unsupported scheme for field: %(scheme), '
                                'valid values are %(vals)') %
                                {'scheme': scheme,
                                 'vals': ', '.join(
                                      self._scheme_data_sources.keys())})
        try:
            self._path_expr = jsonpath.parse(path)
        except Exception as exc:
            raise utils.Error(_('Unable to parse JSON path for URI %(uri)s: '
                                '%(error)s') % {'uri': uri, 'error': exc})
    
    @property
    def data(self):
        return [x.value for x in self._path_expr.find(self._data)]

    @classmethod
    def _parse_uri(cls, uri):
        """Parse URI, extracting the scheme and path.
    
        Parse URI against the allowed schemes, using the default if no scheme
        was provided.
    
        :param uri: scheme + separator + path, e.g. "data://foo"
        :return: tuple (scheme, path)
        """
        try:
            index = uri.index(cls._URI_SEPARATOR)
        except ValueError:
            scheme = cls._DEFAULT_SCHEME
            path = uri
        else:
            scheme = path[:index]
            path = path[index + len(cls._URI_SEPARATOR):]
        return scheme, path


class IntrospectionRule(object):
    """High-level class representing an introspection rule."""

    def __init__(self, uuid, conditions, actions, description):
        """Create rule object from database data."""
        self._uuid = uuid
        self._conditions = conditions
        self._actions = actions
        self._description = description

    def as_dict(self, short=False):
        result = {
            'uuid': self._uuid,
            'description': self._description,
        }

        if not short:
            result['conditions'] = [c.as_dict() for c in self._conditions]
            result['actions'] = [a.as_dict() for a in self._actions]

        return result

    @property
    def description(self):
        return self._description or self._uuid

    def filter_by_conditions(self, node_info, values):
        """Filter out Ironic objects that don't pass the conditions.

        :param node_info: A NodeInfo object
        :param values: The values to check against the conditions
        :returns: A list of Ironic objects
        """
        LOG.debug('Checking rule "%s"', self.description,
                  node_info=node_info, data=data)
        ext_mgr = plugins_base.rule_conditions_manager()

        for cond in self._conditions:
            source_data = SourceData(node_info, data, cond.field)
            if not field_values:
                if cond_ext.ALLOW_NONE:
                    LOG.debug('Field with JSON path %s was not found in data',
                              cond.field, node_info=node_info, data=data)
                    field_values = [None]
                else:
                    LOG.info('Field with JSON path %(path)s was not found '
                             'in data, rule "%(rule)s" will not '
                             'be applied',
                             {'path': cond.field, 'rule': self.description},
                             node_info=node_info, data=data)
                    return False

            for value in field_values:
                result = cond_ext.check(node_info, value, cond.params)
                if cond.invert:
                    result = not result

                if (cond.multiple == 'first'
                        or (cond.multiple == 'all' and not result)
                        or (cond.multiple == 'any' and result)):
                    break

            if not result:
                LOG.info('Rule "%(rule)s" will not be applied: condition '
                         '%(field)s %(op)s %(params)s failed',
                         {'rule': self.description, 'field': cond.field,
                          'op': cond.op, 'params': cond.params},
                         node_info=node_info, data=data)
                return False

        LOG.info('Rule "%s" will be applied', self.description,
                 node_info=node_info, data=data)
        return True

    def apply_actions(self, node_info, data=None):
        """Run actions on a node.

        :param node_info: NodeInfo instance
        :param data: introspection data
        """
        LOG.debug('Running actions for rule "%s"', self.description,
                  node_info=node_info, data=data)

        ext_mgr = plugins_base.rule_actions_manager()
        for act in self._actions:
            ext = ext_mgr[act.action].obj

            for formatted_param in ext.FORMATTED_PARAMS:
                value = act.params.get(formatted_param)
                if not value or not isinstance(value, six.string_types):
                    continue

                # NOTE(aarefiev): verify provided value with introspection
                # data format specifications.
                # TODO(aarefiev): simple verify on import rule time.
                try:
                    act.params[formatted_param] = value.format(data=data)
                except KeyError as e:
                    raise utils.Error(_('Invalid formatting variable key '
                                        'provided: %s') % e,
                                      node_info=node_info, data=data)

            LOG.debug('Running action `%(action)s %(params)s`',
                      {'action': act.action, 'params': act.params},
                      node_info=node_info, data=data)
            ext.apply(node_info, act.params)

        LOG.debug('Successfully applied actions',
                  node_info=node_info, data=data)


def create(conditions_json, actions_json, uuid=None,
           description=None):
    """Create a new rule in database.

    :param conditions_json: list of dicts with the following keys:
                            * op - operator
                            * field - JSON path to field to compare
                            Other keys are stored as is.
    :param actions_json: list of dicts with the following keys:
                         * action - action type
                         Other keys are stored as is.
    :param uuid: rule UUID, will be generated if empty
    :param description: human-readable rule description
    :returns: new IntrospectionRule object
    :raises: utils.Error on failure
    """
    uuid = uuid or uuidutils.generate_uuid()
    LOG.debug('Creating rule %(uuid)s with description "%(descr)s", '
              'conditions %(conditions)s and actions %(actions)s',
              {'uuid': uuid, 'descr': description,
               'conditions': conditions_json, 'actions': actions_json})

    try:
        jsonschema.validate(conditions_json, conditions_schema())
    except jsonschema.ValidationError as exc:
        raise utils.Error(_('Validation failed for conditions: %s') % exc)

    try:
        jsonschema.validate(actions_json, actions_schema())
    except jsonschema.ValidationError as exc:
        raise utils.Error(_('Validation failed for actions: %s') % exc)

    cond_mgr = plugins_base.rule_conditions_manager()
    act_mgr = plugins_base.rule_actions_manager()

    conditions = []
    reserved_params = {'op', 'field', 'multiple', 'invert'}
    for cond_json in conditions_json:
        field = cond_json['field']

        plugin = cond_mgr[cond_json['op']].obj
        params = {k: v for k, v in cond_json.items()
                  if k not in reserved_params}
        try:
            plugin.validate(params)
        except ValueError as exc:
            raise utils.Error(_('Invalid parameters for operator %(op)s: '
                                '%(error)s') %
                              {'op': cond_json['op'], 'error': exc})

        conditions.append((cond_json['field'],
                           cond_json['op'],
                           cond_json.get('multiple', 'any'),
                           cond_json.get('invert', False),
                           params))

    actions = []
    for action_json in actions_json:
        plugin = act_mgr[action_json['action']].obj
        params = {k: v for k, v in action_json.items() if k != 'action'}
        try:
            plugin.validate(params)
        except ValueError as exc:
            raise utils.Error(_('Invalid parameters for action %(act)s: '
                                '%(error)s') %
                              {'act': action_json['action'], 'error': exc})

        actions.append((action_json['action'], params))

    try:
        with db.ensure_transaction() as session:
            rule = db.Rule(uuid=uuid, description=description,
                           disabled=False, created_at=timeutils.utcnow())

            for field, op, multiple, invert, params in conditions:
                rule.conditions.append(db.RuleCondition(op=op,
                                                        field=field,
                                                        multiple=multiple,
                                                        invert=invert,
                                                        params=params))

            for action, params in actions:
                rule.actions.append(db.RuleAction(action=action,
                                                  params=params))

            rule.save(session)
    except db_exc.DBDuplicateEntry as exc:
        LOG.error('Database integrity error %s when '
                  'creating a rule', exc)
        raise utils.Error(_('Rule with UUID %s already exists') % uuid,
                          code=409)

    LOG.info('Created rule %(uuid)s with description "%(descr)s"',
             {'uuid': uuid, 'descr': description})
    return IntrospectionRule(uuid=uuid,
                             conditions=rule.conditions,
                             actions=rule.actions,
                             description=description)


def get(uuid):
    """Get a rule by its UUID."""
    try:
        rule = db.model_query(db.Rule).filter_by(uuid=uuid).one()
    except orm.exc.NoResultFound:
        raise utils.Error(_('Rule %s was not found') % uuid, code=404)

    return IntrospectionRule(uuid=rule.uuid, actions=rule.actions,
                             conditions=rule.conditions,
                             description=rule.description)


def get_all():
    """List all rules."""
    query = db.model_query(db.Rule).order_by(db.Rule.created_at)
    return [IntrospectionRule(uuid=rule.uuid, actions=rule.actions,
                              conditions=rule.conditions,
                              description=rule.description)
            for rule in query]


def delete(uuid):
    """Delete a rule by its UUID."""
    with db.ensure_transaction() as session:
        db.model_query(db.RuleAction,
                       session=session).filter_by(rule=uuid).delete()
        db.model_query(db.RuleCondition,
                       session=session) .filter_by(rule=uuid).delete()
        count = (db.model_query(db.Rule, session=session)
                 .filter_by(uuid=uuid).delete())
        if not count:
            raise utils.Error(_('Rule %s was not found') % uuid, code=404)

    LOG.info('Introspection rule %s was deleted', uuid)


def delete_all():
    """Delete all rules."""
    with db.ensure_transaction() as session:
        db.model_query(db.RuleAction, session=session).delete()
        db.model_query(db.RuleCondition, session=session).delete()
        db.model_query(db.Rule, session=session).delete()

    LOG.info('All introspection rules were deleted')


def apply(node_info, data):
    """Apply rules to a node."""
    rules = get_all()
    if not rules:
        LOG.debug('No custom introspection rules to apply',
                  node_info=node_info, data=data)
        return

    LOG.debug('Applying custom introspection rules',
              node_info=node_info, data=data)

    to_apply = []
    for rule in rules:
        filtered_ironic_objs = rule.filter_by_conditions(node_info, data)
        for obj in filtered_ironic_objs:
            to_apply.append((rule, obj))

    if to_apply:
        LOG.debug('Running actions', node_info=node_info, data=data)
        for rule in to_apply:
            rule.apply_actions(node_info, data=data)
    else:
        LOG.debug('No actions to apply', node_info=node_info, data=data)

    LOG.info('Successfully applied custom introspection rules',
             node_info=node_info, data=data)
