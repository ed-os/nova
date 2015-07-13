#    Copyright 2013 IBM Corp.
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

from collections import OrderedDict
import contextlib
import copy
import datetime
import hashlib
import inspect
import os
import pprint

import fixtures
import mock
from oslo_log import log
from oslo_utils import timeutils
from oslo_versionedobjects import exception as ovo_exc
from oslo_versionedobjects import fixture
import six
from testtools import matchers

from nova import context
from nova import exception
from nova import objects
from nova.objects import base
from nova.objects import fields
from nova import test
from nova.tests import fixtures as nova_fixtures
from nova.tests.unit import fake_notifier
from nova import utils


LOG = log.getLogger(__name__)


class MyOwnedObject(base.NovaPersistentObject, base.NovaObject):
    VERSION = '1.0'
    fields = {'baz': fields.IntegerField()}


class MyObj(base.NovaPersistentObject, base.NovaObject,
            base.NovaObjectDictCompat):
    VERSION = '1.6'
    fields = {'foo': fields.IntegerField(default=1),
              'bar': fields.StringField(),
              'missing': fields.StringField(),
              'readonly': fields.IntegerField(read_only=True),
              'rel_object': fields.ObjectField('MyOwnedObject', nullable=True),
              'rel_objects': fields.ListOfObjectsField('MyOwnedObject',
                                                       nullable=True),
              'mutable_default': fields.ListOfStringsField(default=[]),
              }

    @staticmethod
    def _from_db_object(context, obj, db_obj):
        self = MyObj()
        self.foo = db_obj['foo']
        self.bar = db_obj['bar']
        self.missing = db_obj['missing']
        self.readonly = 1
        self._context = context
        return self

    def obj_load_attr(self, attrname):
        setattr(self, attrname, 'loaded!')

    @base.remotable_classmethod
    def query(cls, context):
        obj = cls(context=context, foo=1, bar='bar')
        obj.obj_reset_changes()
        return obj

    @base.remotable
    def marco(self):
        return 'polo'

    @base.remotable
    def _update_test(self):
        self.bar = 'updated'

    @base.remotable
    def save(self):
        self.obj_reset_changes()

    @base.remotable
    def refresh(self):
        self.foo = 321
        self.bar = 'refreshed'
        self.obj_reset_changes()

    @base.remotable
    def modify_save_modify(self):
        self.bar = 'meow'
        self.save()
        self.foo = 42
        self.rel_object = MyOwnedObject(baz=42)

    def obj_make_compatible(self, primitive, target_version):
        super(MyObj, self).obj_make_compatible(primitive, target_version)
        # NOTE(danms): Simulate an older version that had a different
        # format for the 'bar' attribute
        if target_version == '1.1' and 'bar' in primitive:
            primitive['bar'] = 'old%s' % primitive['bar']


class MyObjDiffVers(MyObj):
    VERSION = '1.5'

    @classmethod
    def obj_name(cls):
        return 'MyObj'


class MyObj2(object):
    fields = {
        'bar': fields.StringField(),
    }

    @classmethod
    def obj_name(cls):
        return 'MyObj'

    @base.remotable_classmethod
    def query(cls, *args, **kwargs):
        pass


class RandomMixInWithNoFields(object):
    """Used to test object inheritance using a mixin that has no fields."""
    pass


@base.NovaObjectRegistry.register_if(False)
class TestSubclassedObject(RandomMixInWithNoFields, MyObj):
    fields = {'new_field': fields.StringField()}


class TestObjToPrimitive(test.NoDBTestCase):

    def test_obj_to_primitive_list(self):
        @base.NovaObjectRegistry.register_if(False)
        class MyObjElement(base.NovaObject):
            fields = {'foo': fields.IntegerField()}

            def __init__(self, foo):
                super(MyObjElement, self).__init__()
                self.foo = foo

        @base.NovaObjectRegistry.register_if(False)
        class MyList(base.ObjectListBase, base.NovaObject):
            fields = {'objects': fields.ListOfObjectsField('MyObjElement')}

        mylist = MyList()
        mylist.objects = [MyObjElement(1), MyObjElement(2), MyObjElement(3)]
        self.assertEqual([1, 2, 3],
                         [x['foo'] for x in base.obj_to_primitive(mylist)])

    def test_obj_to_primitive_dict(self):
        base.NovaObjectRegistry.register(MyObj)
        myobj = MyObj(foo=1, bar='foo')
        self.assertEqual({'foo': 1, 'bar': 'foo'},
                         base.obj_to_primitive(myobj))

    def test_obj_to_primitive_recursive(self):
        base.NovaObjectRegistry.register(MyObj)

        class MyList(base.ObjectListBase, base.NovaObject):
            fields = {'objects': fields.ListOfObjectsField('MyObj')}

        mylist = MyList(objects=[MyObj(), MyObj()])
        for i, value in enumerate(mylist):
            value.foo = i
        self.assertEqual([{'foo': 0}, {'foo': 1}],
                         base.obj_to_primitive(mylist))

    def test_obj_to_primitive_with_ip_addr(self):
        @base.NovaObjectRegistry.register_if(False)
        class TestObject(base.NovaObject):
            fields = {'addr': fields.IPAddressField(),
                      'cidr': fields.IPNetworkField()}

        obj = TestObject(addr='1.2.3.4', cidr='1.1.1.1/16')
        self.assertEqual({'addr': '1.2.3.4', 'cidr': '1.1.1.1/16'},
                         base.obj_to_primitive(obj))


class TestObjMakeList(test.NoDBTestCase):

    def test_obj_make_list(self):
        class MyList(base.ObjectListBase, base.NovaObject):
            fields = {
                'objects': fields.ListOfObjectsField('MyObj'),
            }

        db_objs = [{'foo': 1, 'bar': 'baz', 'missing': 'banana'},
                   {'foo': 2, 'bar': 'bat', 'missing': 'apple'},
                   ]
        mylist = base.obj_make_list('ctxt', MyList(), MyObj, db_objs)
        self.assertEqual(2, len(mylist))
        self.assertEqual('ctxt', mylist._context)
        for index, item in enumerate(mylist):
            self.assertEqual(db_objs[index]['foo'], item.foo)
            self.assertEqual(db_objs[index]['bar'], item.bar)
            self.assertEqual(db_objs[index]['missing'], item.missing)


def compare_obj(test, obj, db_obj, subs=None, allow_missing=None,
                comparators=None):
    """Compare a NovaObject and a dict-like database object.

    This automatically converts TZ-aware datetimes and iterates over
    the fields of the object.

    :param:test: The TestCase doing the comparison
    :param:obj: The NovaObject to examine
    :param:db_obj: The dict-like database object to use as reference
    :param:subs: A dict of objkey=dbkey field substitutions
    :param:allow_missing: A list of fields that may not be in db_obj
    :param:comparators: Map of comparator functions to use for certain fields
    """

    if subs is None:
        subs = {}
    if allow_missing is None:
        allow_missing = []
    if comparators is None:
        comparators = {}

    for key in obj.fields:
        if key in allow_missing and not obj.obj_attr_is_set(key):
            continue
        obj_val = getattr(obj, key)
        db_key = subs.get(key, key)
        db_val = db_obj[db_key]
        if isinstance(obj_val, datetime.datetime):
            obj_val = obj_val.replace(tzinfo=None)

        if key in comparators:
            comparator = comparators[key]
            comparator(db_val, obj_val)
        else:
            test.assertEqual(db_val, obj_val)


class _BaseTestCase(test.TestCase):
    def setUp(self):
        super(_BaseTestCase, self).setUp()
        self.remote_object_calls = list()
        self.user_id = 'fake-user'
        self.project_id = 'fake-project'
        self.context = context.RequestContext(self.user_id, self.project_id)
        fake_notifier.stub_notifier(self.stubs)
        self.addCleanup(fake_notifier.reset)

        # NOTE(danms): register these here instead of at import time
        # so that they're not always present
        base.NovaObjectRegistry.register(MyObj)
        base.NovaObjectRegistry.register(MyObjDiffVers)
        base.NovaObjectRegistry.register(MyOwnedObject)

    def compare_obj(self, obj, db_obj, subs=None, allow_missing=None,
                    comparators=None):
        compare_obj(self, obj, db_obj, subs=subs, allow_missing=allow_missing,
                    comparators=comparators)

    def str_comparator(self, expected, obj_val):
        """Compare an object field to a string in the db by performing
        a simple coercion on the object field value.
        """
        self.assertEqual(expected, str(obj_val))

    def assertNotIsInstance(self, obj, cls, msg=None):
        """Python < v2.7 compatibility.  Assert 'not isinstance(obj, cls)."""
        try:
            f = super(_BaseTestCase, self).assertNotIsInstance
        except AttributeError:
            self.assertThat(obj,
                            matchers.Not(matchers.IsInstance(cls)),
                            message=msg or '')
        else:
            f(obj, cls, msg=msg)


class _LocalTest(_BaseTestCase):
    def setUp(self):
        super(_LocalTest, self).setUp()
        # Just in case
        self.useFixture(nova_fixtures.IndirectionAPIFixture(None))


@contextlib.contextmanager
def things_temporarily_local():
    # Temporarily go non-remote so the conductor handles
    # this request directly
    _api = base.NovaObject.indirection_api
    base.NovaObject.indirection_api = None
    yield
    base.NovaObject.indirection_api = _api


class FakeIndirectionHack(fixture.FakeIndirectionAPI):
    def object_action(self, context, objinst, objmethod, args, kwargs):
        objinst = self._ser.deserialize_entity(
            context, self._ser.serialize_entity(
                context, objinst))
        objmethod = six.text_type(objmethod)
        args = self._ser.deserialize_entity(
            None, self._ser.serialize_entity(None, args))
        kwargs = self._ser.deserialize_entity(
            None, self._ser.serialize_entity(None, kwargs))
        original = objinst.obj_clone()
        with mock.patch('nova.objects.base.NovaObject.'
                        'indirection_api', new=None):
            result = getattr(objinst, objmethod)(*args, **kwargs)
        updates = self._get_changes(original, objinst)
        updates['obj_what_changed'] = objinst.obj_what_changed()
        return updates, result

    def object_class_action(self, context, objname, objmethod, objver,
                            args, kwargs):
        objname = six.text_type(objname)
        objmethod = six.text_type(objmethod)
        objver = six.text_type(objver)
        args = self._ser.deserialize_entity(
            None, self._ser.serialize_entity(None, args))
        kwargs = self._ser.deserialize_entity(
            None, self._ser.serialize_entity(None, kwargs))
        cls = base.NovaObject.obj_class_from_name(objname, objver)
        with mock.patch('nova.objects.base.NovaObject.'
                        'indirection_api', new=None):
            result = getattr(cls, objmethod)(context, *args, **kwargs)
        return (base.NovaObject.obj_from_primitive(
            result.obj_to_primitive(target_version=objver),
            context=context)
            if isinstance(result, base.NovaObject) else result)


class IndirectionFixture(fixtures.Fixture):
    def setUp(self):
        super(IndirectionFixture, self).setUp()
        ser = base.NovaObjectSerializer()
        self.indirection_api = FakeIndirectionHack(serializer=ser)
        self.useFixture(fixtures.MonkeyPatch(
            'nova.objects.base.NovaObject.indirection_api',
            self.indirection_api))


class _RemoteTest(_BaseTestCase):
    def setUp(self):
        super(_RemoteTest, self).setUp()
        self.useFixture(IndirectionFixture())


class _TestObject(object):
    def test_object_attrs_in_init(self):
        # Spot check a few
        objects.Instance
        objects.InstanceInfoCache
        objects.SecurityGroup
        # Now check the test one in this file. Should be newest version
        self.assertEqual('1.6', objects.MyObj.VERSION)

    def test_hydration_type_error(self):
        primitive = {'nova_object.name': 'MyObj',
                     'nova_object.namespace': 'nova',
                     'nova_object.version': '1.5',
                     'nova_object.data': {'foo': 'a'}}
        self.assertRaises(ValueError, MyObj.obj_from_primitive, primitive)

    def test_hydration(self):
        primitive = {'nova_object.name': 'MyObj',
                     'nova_object.namespace': 'nova',
                     'nova_object.version': '1.5',
                     'nova_object.data': {'foo': 1}}
        real_method = MyObj._obj_from_primitive

        def _obj_from_primitive(*args):
            return real_method(*args)

        with mock.patch.object(MyObj, '_obj_from_primitive') as ofp:
            ofp.side_effect = _obj_from_primitive
            obj = MyObj.obj_from_primitive(primitive)
            ofp.assert_called_once_with(None, '1.5', primitive)
        self.assertEqual(obj.foo, 1)

    def test_hydration_version_different(self):
        primitive = {'nova_object.name': 'MyObj',
                     'nova_object.namespace': 'nova',
                     'nova_object.version': '1.2',
                     'nova_object.data': {'foo': 1}}
        obj = MyObj.obj_from_primitive(primitive)
        self.assertEqual(obj.foo, 1)
        self.assertEqual('1.2', obj.VERSION)

    def test_hydration_bad_ns(self):
        primitive = {'nova_object.name': 'MyObj',
                     'nova_object.namespace': 'foo',
                     'nova_object.version': '1.5',
                     'nova_object.data': {'foo': 1}}
        self.assertRaises(exception.UnsupportedObjectError,
                          MyObj.obj_from_primitive, primitive)

    def test_hydration_additional_unexpected_stuff(self):
        primitive = {'nova_object.name': 'MyObj',
                     'nova_object.namespace': 'nova',
                     'nova_object.version': '1.5.1',
                     'nova_object.data': {
                         'foo': 1,
                         'unexpected_thing': 'foobar'}}
        obj = MyObj.obj_from_primitive(primitive)
        self.assertEqual(1, obj.foo)
        self.assertFalse(hasattr(obj, 'unexpected_thing'))
        # NOTE(danms): If we call obj_from_primitive() directly
        # with a version containing .z, we'll get that version
        # in the resulting object. In reality, when using the
        # serializer, we'll get that snipped off (tested
        # elsewhere)
        self.assertEqual('1.5.1', obj.VERSION)

    def test_dehydration(self):
        expected = {'nova_object.name': 'MyObj',
                    'nova_object.namespace': 'nova',
                    'nova_object.version': '1.6',
                    'nova_object.data': {'foo': 1}}
        obj = MyObj(foo=1)
        obj.obj_reset_changes()
        self.assertEqual(obj.obj_to_primitive(), expected)

    def test_object_property(self):
        obj = MyObj(foo=1)
        self.assertEqual(obj.foo, 1)

    def test_object_property_type_error(self):
        obj = MyObj()

        def fail():
            obj.foo = 'a'
        self.assertRaises(ValueError, fail)

    def test_load(self):
        obj = MyObj()
        self.assertEqual(obj.bar, 'loaded!')

    def test_load_in_base(self):
        @base.NovaObjectRegistry.register_if(False)
        class Foo(base.NovaObject):
            fields = {'foobar': fields.IntegerField()}
        obj = Foo()
        with self.assertRaisesRegex(NotImplementedError, ".*foobar.*"):
            obj.foobar

    def test_loaded_in_primitive(self):
        obj = MyObj(foo=1)
        obj.obj_reset_changes()
        self.assertEqual(obj.bar, 'loaded!')
        expected = {'nova_object.name': 'MyObj',
                    'nova_object.namespace': 'nova',
                    'nova_object.version': '1.6',
                    'nova_object.changes': ['bar'],
                    'nova_object.data': {'foo': 1,
                                         'bar': 'loaded!'}}
        self.assertEqual(obj.obj_to_primitive(), expected)

    def test_changes_in_primitive(self):
        obj = MyObj(foo=123)
        self.assertEqual(obj.obj_what_changed(), set(['foo']))
        primitive = obj.obj_to_primitive()
        self.assertIn('nova_object.changes', primitive)
        obj2 = MyObj.obj_from_primitive(primitive)
        self.assertEqual(obj2.obj_what_changed(), set(['foo']))
        obj2.obj_reset_changes()
        self.assertEqual(obj2.obj_what_changed(), set())

    def test_obj_class_from_name(self):
        obj = base.NovaObject.obj_class_from_name('MyObj', '1.5')
        self.assertEqual('1.5', obj.VERSION)

    def test_obj_class_from_name_latest_compatible(self):
        obj = base.NovaObject.obj_class_from_name('MyObj', '1.1')
        self.assertEqual('1.6', obj.VERSION)

    def test_unknown_objtype(self):
        self.assertRaises(exception.UnsupportedObjectError,
                          base.NovaObject.obj_class_from_name, 'foo', '1.0')

    def test_obj_class_from_name_supported_version(self):
        error = None
        try:
            base.NovaObject.obj_class_from_name('MyObj', '1.25')
        except exception.IncompatibleObjectVersion as ex:
            error = ex

        self.assertIsNotNone(error)
        self.assertEqual('1.6', error.kwargs['supported'])

    def test_orphaned_object(self):
        obj = MyObj.query(self.context)
        obj._context = None
        self.assertRaises(exception.OrphanedObjectError,
                          obj._update_test)

    def test_changed_1(self):
        obj = MyObj.query(self.context)
        obj.foo = 123
        self.assertEqual(obj.obj_what_changed(), set(['foo']))
        obj._update_test()
        self.assertEqual(obj.obj_what_changed(), set(['foo', 'bar']))
        self.assertEqual(obj.foo, 123)

    def test_changed_2(self):
        obj = MyObj.query(self.context)
        obj.foo = 123
        self.assertEqual(obj.obj_what_changed(), set(['foo']))
        obj.save()
        self.assertEqual(obj.obj_what_changed(), set([]))
        self.assertEqual(obj.foo, 123)

    def test_changed_3(self):
        obj = MyObj.query(self.context)
        obj.foo = 123
        self.assertEqual(obj.obj_what_changed(), set(['foo']))
        obj.refresh()
        self.assertEqual(obj.obj_what_changed(), set([]))
        self.assertEqual(obj.foo, 321)
        self.assertEqual(obj.bar, 'refreshed')

    def test_changed_4(self):
        obj = MyObj.query(self.context)
        obj.bar = 'something'
        self.assertEqual(obj.obj_what_changed(), set(['bar']))
        obj.modify_save_modify()
        self.assertEqual(obj.obj_what_changed(), set(['foo', 'rel_object']))
        self.assertEqual(obj.foo, 42)
        self.assertEqual(obj.bar, 'meow')
        self.assertIsInstance(obj.rel_object, MyOwnedObject)

    def test_changed_with_sub_object(self):
        @base.NovaObjectRegistry.register_if(False)
        class ParentObject(base.NovaObject):
            fields = {'foo': fields.IntegerField(),
                      'bar': fields.ObjectField('MyObj'),
                      }
        obj = ParentObject()
        self.assertEqual(set(), obj.obj_what_changed())
        obj.foo = 1
        self.assertEqual(set(['foo']), obj.obj_what_changed())
        bar = MyObj()
        obj.bar = bar
        self.assertEqual(set(['foo', 'bar']), obj.obj_what_changed())
        obj.obj_reset_changes()
        self.assertEqual(set(), obj.obj_what_changed())
        bar.foo = 1
        self.assertEqual(set(['bar']), obj.obj_what_changed())

    def test_static_result(self):
        obj = MyObj.query(self.context)
        self.assertEqual(obj.bar, 'bar')
        result = obj.marco()
        self.assertEqual(result, 'polo')

    def test_updates(self):
        obj = MyObj.query(self.context)
        self.assertEqual(obj.foo, 1)
        obj._update_test()
        self.assertEqual(obj.bar, 'updated')

    def test_base_attributes(self):
        dt = datetime.datetime(1955, 11, 5)
        obj = MyObj(created_at=dt, updated_at=dt, deleted_at=None,
                    deleted=False)
        expected = {'nova_object.name': 'MyObj',
                    'nova_object.namespace': 'nova',
                    'nova_object.version': '1.6',
                    'nova_object.changes':
                        ['deleted', 'created_at', 'deleted_at', 'updated_at'],
                    'nova_object.data':
                        {'created_at': timeutils.isotime(dt),
                         'updated_at': timeutils.isotime(dt),
                         'deleted_at': None,
                         'deleted': False,
                         }
                    }
        actual = obj.obj_to_primitive()
        self.assertJsonEqual(actual, expected)

    def test_contains(self):
        obj = MyObj()
        self.assertNotIn('foo', obj)
        obj.foo = 1
        self.assertIn('foo', obj)
        self.assertNotIn('does_not_exist', obj)

    def test_obj_attr_is_set(self):
        obj = MyObj(foo=1)
        self.assertTrue(obj.obj_attr_is_set('foo'))
        self.assertFalse(obj.obj_attr_is_set('bar'))
        self.assertRaises(AttributeError, obj.obj_attr_is_set, 'bang')

    def test_obj_reset_changes_recursive(self):
        obj = MyObj(rel_object=MyOwnedObject(baz=123),
                    rel_objects=[MyOwnedObject(baz=456)])
        self.assertEqual(set(['rel_object', 'rel_objects']),
                         obj.obj_what_changed())
        obj.obj_reset_changes()
        self.assertEqual(set(['rel_object']), obj.obj_what_changed())
        self.assertEqual(set(['baz']), obj.rel_object.obj_what_changed())
        self.assertEqual(set(['baz']), obj.rel_objects[0].obj_what_changed())
        obj.obj_reset_changes(recursive=True, fields=['foo'])
        self.assertEqual(set(['rel_object']), obj.obj_what_changed())
        self.assertEqual(set(['baz']), obj.rel_object.obj_what_changed())
        self.assertEqual(set(['baz']), obj.rel_objects[0].obj_what_changed())
        obj.obj_reset_changes(recursive=True)
        self.assertEqual(set([]), obj.rel_object.obj_what_changed())
        self.assertEqual(set([]), obj.obj_what_changed())

    def test_get(self):
        obj = MyObj(foo=1)
        # Foo has value, should not get the default
        self.assertEqual(obj.get('foo', 2), 1)
        # Foo has value, should return the value without error
        self.assertEqual(obj.get('foo'), 1)
        # Bar is not loaded, so we should get the default
        self.assertEqual(obj.get('bar', 'not-loaded'), 'not-loaded')
        # Bar without a default should lazy-load
        self.assertEqual(obj.get('bar'), 'loaded!')
        # Bar now has a default, but loaded value should be returned
        self.assertEqual(obj.get('bar', 'not-loaded'), 'loaded!')
        # Invalid attribute should raise AttributeError
        self.assertRaises(AttributeError, obj.get, 'nothing')
        # ...even with a default
        self.assertRaises(AttributeError, obj.get, 'nothing', 3)

    def test_object_inheritance(self):
        base_fields = base.NovaPersistentObject.fields.keys()
        myobj_fields = (['foo', 'bar', 'missing',
                         'readonly', 'rel_object',
                         'rel_objects', 'mutable_default'] +
                        list(base_fields))
        myobj3_fields = ['new_field']
        self.assertTrue(issubclass(TestSubclassedObject, MyObj))
        self.assertEqual(len(myobj_fields), len(MyObj.fields))
        self.assertEqual(set(myobj_fields), set(MyObj.fields.keys()))
        self.assertEqual(len(myobj_fields) + len(myobj3_fields),
                         len(TestSubclassedObject.fields))
        self.assertEqual(set(myobj_fields) | set(myobj3_fields),
                         set(TestSubclassedObject.fields.keys()))

    def test_obj_as_admin(self):
        obj = MyObj(context=self.context)

        def fake(*args, **kwargs):
            self.assertTrue(obj._context.is_admin)

        with mock.patch.object(obj, 'obj_reset_changes') as mock_fn:
            mock_fn.side_effect = fake
            with obj.obj_as_admin():
                obj.save()
            self.assertTrue(mock_fn.called)

        self.assertFalse(obj._context.is_admin)

    def test_obj_as_admin_orphaned(self):
        def testme():
            obj = MyObj()
            with obj.obj_as_admin():
                pass
        self.assertRaises(exception.OrphanedObjectError, testme)

    def test_obj_alternate_context(self):
        obj = MyObj(context=self.context)
        with obj.obj_alternate_context(mock.sentinel.alt_ctx):
            self.assertEqual(mock.sentinel.alt_ctx,
                             obj._context)
        self.assertEqual(self.context, obj._context)

    def test_get_changes(self):
        obj = MyObj()
        self.assertEqual({}, obj.obj_get_changes())
        obj.foo = 123
        self.assertEqual({'foo': 123}, obj.obj_get_changes())
        obj.bar = 'test'
        self.assertEqual({'foo': 123, 'bar': 'test'}, obj.obj_get_changes())
        obj.obj_reset_changes()
        self.assertEqual({}, obj.obj_get_changes())

    def test_obj_fields(self):
        @base.NovaObjectRegistry.register_if(False)
        class TestObj(base.NovaObject):
            fields = {'foo': fields.IntegerField()}
            obj_extra_fields = ['bar']

            @property
            def bar(self):
                return 'this is bar'

        obj = TestObj()
        self.assertEqual(['foo', 'bar'], obj.obj_fields)

    def test_obj_constructor(self):
        obj = MyObj(context=self.context, foo=123, bar='abc')
        self.assertEqual(123, obj.foo)
        self.assertEqual('abc', obj.bar)
        self.assertEqual(set(['foo', 'bar']), obj.obj_what_changed())

    def test_obj_read_only(self):
        obj = MyObj(context=self.context, foo=123, bar='abc')
        obj.readonly = 1
        self.assertRaises(ovo_exc.ReadOnlyFieldError, setattr,
                          obj, 'readonly', 2)

    def test_obj_mutable_default(self):
        obj = MyObj(context=self.context, foo=123, bar='abc')
        obj.mutable_default = None
        obj.mutable_default.append('s1')
        self.assertEqual(obj.mutable_default, ['s1'])

        obj1 = MyObj(context=self.context, foo=123, bar='abc')
        obj1.mutable_default = None
        obj1.mutable_default.append('s2')
        self.assertEqual(obj1.mutable_default, ['s2'])

    def test_obj_mutable_default_set_default(self):
        obj1 = MyObj(context=self.context, foo=123, bar='abc')
        obj1.obj_set_defaults('mutable_default')
        self.assertEqual(obj1.mutable_default, [])
        obj1.mutable_default.append('s1')
        self.assertEqual(obj1.mutable_default, ['s1'])

        obj2 = MyObj(context=self.context, foo=123, bar='abc')
        obj2.obj_set_defaults('mutable_default')
        self.assertEqual(obj2.mutable_default, [])
        obj2.mutable_default.append('s2')
        self.assertEqual(obj2.mutable_default, ['s2'])

    def test_obj_repr(self):
        obj = MyObj(foo=123)
        self.assertEqual('MyObj(bar=<?>,created_at=<?>,deleted=<?>,'
                         'deleted_at=<?>,foo=123,missing=<?>,'
                         'mutable_default=<?>,readonly=<?>,rel_object=<?>,'
                         'rel_objects=<?>,updated_at=<?>)',
                         repr(obj))

    def test_obj_make_obj_compatible(self):
        subobj = MyOwnedObject(baz=1)
        subobj.VERSION = '1.2'
        obj = MyObj(rel_object=subobj)
        obj.obj_relationships = {
            'rel_object': [('1.5', '1.1'), ('1.7', '1.2')],
        }
        orig_primitive = obj.obj_to_primitive()['nova_object.data']
        with mock.patch.object(subobj, 'obj_make_compatible') as mock_compat:
            primitive = copy.deepcopy(orig_primitive)
            obj._obj_make_obj_compatible(primitive, '1.8', 'rel_object')
            self.assertFalse(mock_compat.called)

        with mock.patch.object(subobj, 'obj_make_compatible') as mock_compat:
            primitive = copy.deepcopy(orig_primitive)
            obj._obj_make_obj_compatible(primitive, '1.7', 'rel_object')
            self.assertFalse(mock_compat.called)

        with mock.patch.object(subobj, 'obj_make_compatible') as mock_compat:
            primitive = copy.deepcopy(orig_primitive)
            obj._obj_make_obj_compatible(primitive, '1.6', 'rel_object')
            mock_compat.assert_called_once_with(
                primitive['rel_object']['nova_object.data'], '1.1')
            self.assertEqual('1.1',
                             primitive['rel_object']['nova_object.version'])

        with mock.patch.object(subobj, 'obj_make_compatible') as mock_compat:
            primitive = copy.deepcopy(orig_primitive)
            obj._obj_make_obj_compatible(primitive, '1.5', 'rel_object')
            mock_compat.assert_called_once_with(
                primitive['rel_object']['nova_object.data'], '1.1')
            self.assertEqual('1.1',
                             primitive['rel_object']['nova_object.version'])

        with mock.patch.object(subobj, 'obj_make_compatible') as mock_compat:
            primitive = copy.deepcopy(orig_primitive)
            obj._obj_make_obj_compatible(primitive, '1.4', 'rel_object')
            self.assertFalse(mock_compat.called)
            self.assertNotIn('rel_object', primitive)

    def test_obj_make_compatible_hits_sub_objects(self):
        subobj = MyOwnedObject(baz=1)
        obj = MyObj(foo=123, rel_object=subobj)
        obj.obj_relationships = {'rel_object': [('1.0', '1.0')]}
        with mock.patch.object(obj, '_obj_make_obj_compatible') as mock_compat:
            obj.obj_make_compatible({'rel_object': 'foo'}, '1.10')
            mock_compat.assert_called_once_with({'rel_object': 'foo'}, '1.10',
                                                'rel_object')

    def test_obj_make_compatible_skips_unset_sub_objects(self):
        obj = MyObj(foo=123)
        obj.obj_relationships = {'rel_object': [('1.0', '1.0')]}
        with mock.patch.object(obj, '_obj_make_obj_compatible') as mock_compat:
            obj.obj_make_compatible({'rel_object': 'foo'}, '1.10')
            self.assertFalse(mock_compat.called)

    def test_obj_make_compatible_complains_about_missing_rules(self):
        subobj = MyOwnedObject(baz=1)
        obj = MyObj(foo=123, rel_object=subobj)
        obj.obj_relationships = {}
        self.assertRaises(exception.ObjectActionError,
                          obj.obj_make_compatible, {}, '1.0')

    def test_obj_make_compatible_doesnt_skip_falsey_sub_objects(self):
        @base.NovaObjectRegistry.register_if(False)
        class MyList(base.ObjectListBase, base.NovaObject):
            VERSION = '1.2'
            fields = {'objects': fields.ListOfObjectsField('MyObjElement')}

        mylist = MyList(objects=[])

        @base.NovaObjectRegistry.register_if(False)
        class MyOwner(base.NovaObject):
            VERSION = '1.2'
            fields = {'mylist': fields.ObjectField('MyList')}
            obj_relationships = {
                'mylist': [('1.1', '1.1')],
            }

        myowner = MyOwner(mylist=mylist)
        primitive = myowner.obj_to_primitive('1.1')
        self.assertIn('mylist', primitive['nova_object.data'])

    def test_obj_make_compatible_handles_list_of_objects(self):
        subobj = MyOwnedObject(baz=1)
        obj = MyObj(rel_objects=[subobj])
        obj.obj_relationships = {'rel_objects': [('1.0', '1.123')]}

        def fake_make_compat(primitive, version):
            self.assertEqual('1.123', version)
            self.assertIn('baz', primitive)

        with mock.patch.object(subobj, 'obj_make_compatible') as mock_mc:
            mock_mc.side_effect = fake_make_compat
            obj.obj_to_primitive('1.0')
            self.assertTrue(mock_mc.called)

    def test_delattr(self):
        obj = MyObj(bar='foo')
        del obj.bar

        # Should appear unset now
        self.assertFalse(obj.obj_attr_is_set('bar'))

        # Make sure post-delete, references trigger lazy loads
        self.assertEqual('loaded!', getattr(obj, 'bar'))

    def test_delattr_unset(self):
        obj = MyObj()
        self.assertRaises(AttributeError, delattr, obj, 'bar')


class TestObject(_LocalTest, _TestObject):
    def test_set_defaults(self):
        obj = MyObj()
        obj.obj_set_defaults('foo')
        self.assertTrue(obj.obj_attr_is_set('foo'))
        self.assertEqual(1, obj.foo)

    def test_set_defaults_no_default(self):
        obj = MyObj()
        self.assertRaises(exception.ObjectActionError,
                          obj.obj_set_defaults, 'bar')

    def test_set_all_defaults(self):
        obj = MyObj()
        obj.obj_set_defaults()
        self.assertEqual(set(['deleted', 'foo', 'mutable_default']),
                         obj.obj_what_changed())
        self.assertEqual(1, obj.foo)

    def test_set_defaults_not_overwrite(self):
        # NOTE(danms): deleted defaults to False, so verify that it does
        # not get reset by obj_set_defaults()
        obj = MyObj(deleted=True)
        obj.obj_set_defaults()
        self.assertEqual(1, obj.foo)
        self.assertTrue(obj.deleted)


class TestRemoteObject(_RemoteTest, _TestObject):
    def test_major_version_mismatch(self):
        MyObj2.VERSION = '2.0'
        self.assertRaises(exception.IncompatibleObjectVersion,
                          MyObj2.query, self.context)

    def test_minor_version_greater(self):
        MyObj2.VERSION = '1.7'
        self.assertRaises(exception.IncompatibleObjectVersion,
                          MyObj2.query, self.context)

    def test_minor_version_less(self):
        MyObj2.VERSION = '1.2'
        obj = MyObj2.query(self.context)
        self.assertEqual(obj.bar, 'bar')

    def test_compat(self):
        MyObj2.VERSION = '1.1'
        obj = MyObj2.query(self.context)
        self.assertEqual('oldbar', obj.bar)

    def test_revision_ignored(self):
        MyObj2.VERSION = '1.1.456'
        obj = MyObj2.query(self.context)
        self.assertEqual('bar', obj.bar)


class TestObjectSerializer(_BaseTestCase):
    def test_serialize_entity_primitive(self):
        ser = base.NovaObjectSerializer()
        for thing in (1, 'foo', [1, 2], {'foo': 'bar'}):
            self.assertEqual(thing, ser.serialize_entity(None, thing))

    def test_deserialize_entity_primitive(self):
        ser = base.NovaObjectSerializer()
        for thing in (1, 'foo', [1, 2], {'foo': 'bar'}):
            self.assertEqual(thing, ser.deserialize_entity(None, thing))

    def test_serialize_set_to_list(self):
        ser = base.NovaObjectSerializer()
        self.assertEqual([1, 2], ser.serialize_entity(None, set([1, 2])))

    def _test_deserialize_entity_newer(self, obj_version, backported_to,
                                       my_version='1.6'):
        ser = base.NovaObjectSerializer()
        ser._conductor = mock.Mock()
        ser._conductor.object_backport.return_value = 'backported'

        class MyTestObj(MyObj):
            VERSION = my_version

        base.NovaObjectRegistry.register(MyTestObj)

        obj = MyTestObj()
        obj.VERSION = obj_version
        primitive = obj.obj_to_primitive()
        result = ser.deserialize_entity(self.context, primitive)
        if backported_to is None:
            self.assertFalse(ser._conductor.object_backport.called)
        else:
            self.assertEqual('backported', result)
            ser._conductor.object_backport.assert_called_with(self.context,
                                                              primitive,
                                                              backported_to)

    def test_deserialize_entity_newer_version_backports(self):
        self._test_deserialize_entity_newer('1.25', '1.6')

    def test_deserialize_entity_newer_revision_does_not_backport_zero(self):
        self._test_deserialize_entity_newer('1.6.0', None)

    def test_deserialize_entity_newer_revision_does_not_backport(self):
        self._test_deserialize_entity_newer('1.6.1', None)

    def test_deserialize_entity_newer_version_passes_revision(self):
        self._test_deserialize_entity_newer('1.7', '1.6.1', '1.6.1')

    def test_deserialize_dot_z_with_extra_stuff(self):
        primitive = {'nova_object.name': 'MyObj',
                     'nova_object.namespace': 'nova',
                     'nova_object.version': '1.6.1',
                     'nova_object.data': {
                         'foo': 1,
                         'unexpected_thing': 'foobar'}}
        ser = base.NovaObjectSerializer()
        obj = ser.deserialize_entity(self.context, primitive)
        self.assertEqual(1, obj.foo)
        self.assertFalse(hasattr(obj, 'unexpected_thing'))
        # NOTE(danms): The serializer is where the logic lives that
        # avoids backports for cases where only a .z difference in
        # the received object version is detected. As a result, we
        # end up with a version of what we expected, effectively the
        # .0 of the object.
        self.assertEqual('1.6', obj.VERSION)

    def test_object_serialization(self):
        ser = base.NovaObjectSerializer()
        obj = MyObj()
        primitive = ser.serialize_entity(self.context, obj)
        self.assertIn('nova_object.name', primitive)
        obj2 = ser.deserialize_entity(self.context, primitive)
        self.assertIsInstance(obj2, MyObj)
        self.assertEqual(self.context, obj2._context)

    def test_object_serialization_iterables(self):
        ser = base.NovaObjectSerializer()
        obj = MyObj()
        for iterable in (list, tuple, set):
            thing = iterable([obj])
            primitive = ser.serialize_entity(self.context, thing)
            self.assertEqual(1, len(primitive))
            for item in primitive:
                self.assertNotIsInstance(item, base.NovaObject)
            thing2 = ser.deserialize_entity(self.context, primitive)
            self.assertEqual(1, len(thing2))
            for item in thing2:
                self.assertIsInstance(item, MyObj)
        # dict case
        thing = {'key': obj}
        primitive = ser.serialize_entity(self.context, thing)
        self.assertEqual(1, len(primitive))
        for item in six.itervalues(primitive):
            self.assertNotIsInstance(item, base.NovaObject)
        thing2 = ser.deserialize_entity(self.context, primitive)
        self.assertEqual(1, len(thing2))
        for item in six.itervalues(thing2):
            self.assertIsInstance(item, MyObj)

        # object-action updates dict case
        thing = {'foo': obj.obj_to_primitive()}
        primitive = ser.serialize_entity(self.context, thing)
        self.assertEqual(thing, primitive)
        thing2 = ser.deserialize_entity(self.context, thing)
        self.assertIsInstance(thing2['foo'], base.NovaObject)


class TestArgsSerializer(test.NoDBTestCase):
    def setUp(self):
        super(TestArgsSerializer, self).setUp()
        self.now = timeutils.utcnow()
        self.str_now = timeutils.strtime(at=self.now)

    @base.serialize_args
    def _test_serialize_args(self, *args, **kwargs):
        expected_args = ('untouched', self.str_now, self.str_now)
        for index, val in enumerate(args):
            self.assertEqual(expected_args[index], val)

        expected_kwargs = {'a': 'untouched', 'b': self.str_now,
                           'c': self.str_now}
        for key, val in six.iteritems(kwargs):
            self.assertEqual(expected_kwargs[key], val)

    def test_serialize_args(self):
        self._test_serialize_args('untouched', self.now, self.now,
                                  a='untouched', b=self.now, c=self.now)


# NOTE(danms): The hashes in this list should only be changed if
# they come with a corresponding version bump in the affected
# objects
object_data = {
    'Agent': '1.0-c0c092abaceb6f51efe5d82175f15eba',
    'AgentList': '1.0-4f12bf96ca77315e7e023d588fb071f1',
    'Aggregate': '1.1-1ab35c4516f71de0bef7087026ab10d1',
    'AggregateList': '1.2-79689d69db4de545a82fe09f30468c53',
    'BandwidthUsage': '1.2-c6e4c779c7f40f2407e3d70022e3cd1c',
    'BandwidthUsageList': '1.2-77b4d43e641459f464a6aa4d53debd8f',
    'BlockDeviceMapping': '1.13-d44d8d694619e79c172a99b3c1d6261d',
    'BlockDeviceMappingList': '1.14-ff39c726181b66dfdf54d7c73abf5ffb',
    'CellMapping': '1.0-7f1a7e85a22bbb7559fc730ab658b9bd',
    'ComputeNode': '1.11-71784d2e6f2814ab467d4e0f69286843',
    'ComputeNodeList': '1.11-8d269636229e8a39fef1c3514f77d0c0',
    'DNSDomain': '1.0-7b0b2dab778454b6a7b6c66afe163a1a',
    'DNSDomainList': '1.0-f876961b1a6afe400b49cf940671db86',
    'EC2Ids': '1.0-474ee1094c7ec16f8ce657595d8c49d9',
    'EC2InstanceMapping': '1.0-a4556eb5c5e94c045fe84f49cf71644f',
    'EC2SnapshotMapping': '1.0-47e7ddabe1af966dce0cfd0ed6cd7cd1',
    'EC2VolumeMapping': '1.0-5b713751d6f97bad620f3378a521020d',
    'FixedIP': '1.11-b5818a33996228fc146f096d1403742c',
    'FixedIPList': '1.11-18c3cc9a716bc12dd35940fe64d8eb7b',
    'Flavor': '1.1-b6bb7a730a79d720344accefafacf7ee',
    'FlavorList': '1.1-d96e87307f94062ce538f77b5e221e13',
    'FloatingIP': '1.7-52a67d52d85eb8b3f324a5b7935a335b',
    'FloatingIPList': '1.8-9a9fe191dc694ea4ff08946be9460718',
    'HostMapping': '1.0-1a3390a696792a552ab7bd31a77ba9ac',
    'HVSpec': '1.0-3999ff70698fc472c2d4d60359949f6b',
    'ImageMeta': '1.1-642d1b2eb3e880a367f37d72dd76162d',
    'ImageMetaProps': '1.1-8fe09b7872538f291649e77375f8ac4c',
    'Instance': '1.21-260d385315d4868b6397c61a13109841',
    'InstanceAction': '1.1-f9f293e526b66fca0d05c3b3a2d13914',
    'InstanceActionEvent': '1.1-e56a64fa4710e43ef7af2ad9d6028b33',
    'InstanceActionEventList': '1.0-c37db4e58b637a857c90fb02284d8f7c',
    'InstanceActionList': '1.0-89266105d853ff9b8f83351776fab788',
    'InstanceExternalEvent': '1.0-33cc4a1bbd0655f68c0ee791b95da7e6',
    'InstanceFault': '1.2-7ef01f16f1084ad1304a513d6d410a38',
    'InstanceFaultList': '1.1-ac4076924f7eb5374a92e4f9db7aa053',
    'InstanceGroup': '1.9-a413a4ec0ff391e3ef0faa4e3e2a96d0',
    'InstanceGroupList': '1.6-1e383df73d9bd224714df83d9a9983bb',
    'InstanceInfoCache': '1.5-cd8b96fefe0fc8d4d337243ba0bf0e1e',
    'InstanceList': '1.18-592dca17aa22feacae9f1ff854e17c79',
    'InstanceMapping': '1.0-47ef26034dfcbea78427565d9177fe50',
    'InstanceMappingList': '1.0-b7b108f6a56bd100c20a3ebd5f3801a1',
    'InstanceNUMACell': '1.2-535ef30e0de2d6a0d26a71bd58ecafc4',
    'InstanceNUMATopology': '1.1-d944a7d6c21e1c773ffdf09c6d025954',
    'InstancePCIRequest': '1.1-b1d75ebc716cb12906d9d513890092bf',
    'InstancePCIRequests': '1.1-fc8d179960869c9af038205a80af2541',
    'KeyPair': '1.3-bfaa2a8b148cdf11e0c72435d9dd097a',
    'KeyPairList': '1.2-60f984184dc5a8eba6e34e20cbabef04',
    'Migration': '1.2-8784125bedcea0a9227318511904e853',
    'MigrationList': '1.2-5e79c0693d7ebe4e9ac03b5db11ab243',
    'MonitorMetric': '1.0-4fe7f3fb1777567883ac842120ec5800',
    'MonitorMetricList': '1.0-1b54e51ad0fc1f3a8878f5010e7e16dc',
    'NUMACell': '1.2-74fc993ac5c83005e76e34e8487f1c05',
    'NUMAPagesTopology': '1.0-c71d86317283266dc8364c149155e48e',
    'NUMATopology': '1.2-c63fad38be73b6afd04715c9c1b29220',
    'NUMATopologyLimits': '1.0-9463e0edd40f64765ae518a539b9dfd2',
    'Network': '1.2-a977ab383aa462a479b2fae8211a5dde',
    'NetworkList': '1.2-b2ae592657f06f6edce4c616821abcf8',
    'NetworkRequest': '1.1-7a3e4ca2ce1e7b62d8400488f2f2b756',
    'NetworkRequestList': '1.1-ea2a8e1c1ecf3608af2956e657adeb4c',
    'PciDevice': '1.3-4d43db45e3978fca4280f696633c7c20',
    'PciDeviceList': '1.1-2b8b6d0cf622c58543c5dec50c7e877c',
    'PciDevicePool': '1.1-3f5ddc3ff7bfa14da7f6c7e9904cc000',
    'PciDevicePoolList': '1.1-ea2a8e1c1ecf3608af2956e657adeb4c',
    'Quotas': '1.2-1fe4cd50593aaf5d36a6dc5ab3f98fb3',
    'QuotasNoOp': '1.2-e041ddeb7dc8188ca71706f78aad41c1',
    'S3ImageMapping': '1.0-7dd7366a890d82660ed121de9092276e',
    'SecurityGroup': '1.1-0e1b9ba42fe85c13c1437f8b74bdb976',
    'SecurityGroupList': '1.0-a3bb51998e7d2a95b3e613111e853817',
    'SecurityGroupRule': '1.1-ae1da17b79970012e8536f88cb3c6b29',
    'SecurityGroupRuleList': '1.1-521f1aeb7b0cc00d026175509289d020',
    'Service': '1.13-bc6c9671a91439e08224c2652da5fc4c',
    'ServiceList': '1.11-d1728430a30700c143e542b7c75f65b0',
    'TaskLog': '1.0-78b0534366f29aa3eebb01860fbe18fe',
    'TaskLogList': '1.0-2378c0e2afdbbfaf392f31c1dffa4d25',
    'Tag': '1.1-8b8d7d5b48887651a0e01241672e2963',
    'TagList': '1.1-6263d7242b87010174cf1d4ad49e5148',
    'VirtCPUFeature': '1.0-3310718d8c72309259a6e39bdefe83ee',
    'VirtCPUModel': '1.0-6a5cc9f322729fc70ddc6733bacd57d3',
    'VirtCPUTopology': '1.0-fc694de72e20298f7c6bab1083fd4563',
    'VirtualInterface': '1.0-19921e38cba320f355d56ecbf8f29587',
    'VirtualInterfaceList': '1.0-16a5c18df5574a9405e1a8b350ed8b27',
}


object_relationships = {
    'AgentList': {'Agent': '1.0'},
    'AggregateList': {'Aggregate': '1.1'},
    'BandwidthUsageList': {'BandwidthUsage': '1.2'},
    'BlockDeviceMapping': {'Instance': '1.21'},
    'BlockDeviceMappingList': {'BlockDeviceMapping': '1.13'},
    'ComputeNode': {'HVSpec': '1.0', 'PciDevicePoolList': '1.1'},
    'ComputeNodeList': {'ComputeNode': '1.11'},
    'DNSDomainList': {'DNSDomain': '1.0'},
    'FixedIP': {'Instance': '1.21', 'Network': '1.2',
                'VirtualInterface': '1.0',
                'FloatingIPList': '1.8'},
    'FixedIPList': {'FixedIP': '1.11'},
    'FlavorList': {'Flavor': '1.1'},
    'FloatingIP': {'FixedIP': '1.11'},
    'FloatingIPList': {'FloatingIP': '1.7'},
    'HostMapping': {'CellMapping': '1.0'},
    'ImageMeta': {'ImageMetaProps': '1.1'},
    'Instance': {'InstanceFault': '1.2',
                 'InstanceInfoCache': '1.5',
                 'InstanceNUMATopology': '1.1',
                 'PciDeviceList': '1.1',
                 'TagList': '1.1',
                 'SecurityGroupList': '1.0',
                 'Flavor': '1.1',
                 'InstancePCIRequests': '1.1',
                 'VirtCPUModel': '1.0',
                 'EC2Ids': '1.0',
                 },
    'InstanceActionEventList': {'InstanceActionEvent': '1.1'},
    'InstanceActionList': {'InstanceAction': '1.1'},
    'InstanceFaultList': {'InstanceFault': '1.2'},
    'InstanceGroupList': {'InstanceGroup': '1.9'},
    'InstanceList': {'Instance': '1.21'},
    'InstanceMappingList': {'InstanceMapping': '1.0'},
    'InstanceNUMACell': {'VirtCPUTopology': '1.0'},
    'InstanceNUMATopology': {'InstanceNUMACell': '1.2'},
    'InstancePCIRequests': {'InstancePCIRequest': '1.1'},
    'KeyPairList': {'KeyPair': '1.3'},
    'MigrationList': {'Migration': '1.2'},
    'MonitorMetricList': {'MonitorMetric': '1.0'},
    'NetworkList': {'Network': '1.2'},
    'NetworkRequestList': {'NetworkRequest': '1.1'},
    'NUMACell': {'NUMAPagesTopology': '1.0'},
    'NUMATopology': {'NUMACell': '1.2'},
    'PciDeviceList': {'PciDevice': '1.3'},
    'PciDevicePoolList': {'PciDevicePool': '1.1'},
    'SecurityGroupList': {'SecurityGroup': '1.1'},
    'SecurityGroupRule': {'SecurityGroup': '1.1'},
    'SecurityGroupRuleList': {'SecurityGroupRule': '1.1'},
    'Service': {'ComputeNode': '1.11'},
    'ServiceList': {'Service': '1.13'},
    'TagList': {'Tag': '1.1'},
    'TaskLogList': {'TaskLog': '1.0'},
    'VirtCPUModel': {'VirtCPUFeature': '1.0', 'VirtCPUTopology': '1.0'},
    'VirtualInterfaceList': {'VirtualInterface': '1.0'}
}


class TestObjectVersions(test.NoDBTestCase):
    @staticmethod
    def _is_method(thing):
        # NOTE(dims): In Python3, The concept of 'unbound methods' has
        # been removed from the language. When referencing a method
        # as a class attribute, you now get a plain function object.
        # so let's check for both
        return inspect.isfunction(thing) or inspect.ismethod(thing)

    def _find_remotable_method(self, cls, thing, parent_was_remotable=False):
        """Follow a chain of remotable things down to the original function."""
        if isinstance(thing, classmethod):
            return self._find_remotable_method(cls, thing.__get__(None, cls))
        elif self._is_method(thing) and hasattr(thing, 'remotable'):
            return self._find_remotable_method(cls, thing.original_fn,
                                               parent_was_remotable=True)
        elif parent_was_remotable:
            # We must be the first non-remotable thing underneath a stack of
            # remotable things (i.e. the actual implementation method)
            return thing
        else:
            # This means the top-level thing never hit a remotable layer
            return None

    def _get_fingerprint(self, obj_name):
        obj_classes = base.NovaObjectRegistry.obj_classes()
        obj_class = obj_classes[obj_name][0]
        fields = list(obj_class.fields.items())
        fields.sort()
        methods = []
        for name in dir(obj_class):
            thing = getattr(obj_class, name)
            if self._is_method(thing) or isinstance(thing, classmethod):
                method = self._find_remotable_method(obj_class, thing)
                if method:
                    methods.append((name, inspect.getargspec(method)))
        methods.sort()
        # NOTE(danms): Things that need a version bump are any fields
        # and their types, or the signatures of any remotable methods.
        # Of course, these are just the mechanical changes we can detect,
        # but many other things may require a version bump (method behavior
        # and return value changes, for example).
        if hasattr(obj_class, 'child_versions'):
            relevant_data = (fields, methods,
                             OrderedDict(
                                 sorted(obj_class.child_versions.items())))
        else:
            relevant_data = (fields, methods)
        relevant_data = repr(relevant_data)
        if six.PY3:
            relevant_data = relevant_data.encode('utf-8')
        fingerprint = '%s-%s' % (
        obj_class.VERSION, hashlib.md5(relevant_data).hexdigest())
        return fingerprint

    def test_find_remotable_method(self):
        class MyObject(object):
            @base.remotable
            def my_method(self):
                return 'Hello World!'
        thing = self._find_remotable_method(MyObject,
                                            getattr(MyObject, 'my_method'))
        self.assertIsNotNone(thing)

    def test_versions(self):
        fingerprints = {}
        obj_classes = base.NovaObjectRegistry.obj_classes()
        for obj_name in sorted(obj_classes, key=lambda x: x[0]):
            fingerprints[obj_name] = self._get_fingerprint(obj_name)

        if os.getenv('GENERATE_HASHES'):
            file('object_hashes.txt', 'w').write(
                pprint.pformat(fingerprints))
            raise test.TestingException(
                'Generated hashes in object_hashes.txt')

        stored = set(object_data.items())
        computed = set(fingerprints.items())
        changed = stored.symmetric_difference(computed)
        expected = {}
        actual = {}
        for name, hash in changed:
            expected[name] = object_data.get(name)
            actual[name] = fingerprints.get(name)

        self.assertEqual(expected, actual,
                         'Some objects have changed; please make sure the '
                         'versions have been bumped, and then update their '
                         'hashes here.')

    def _get_object_field_name(self, field):
        if isinstance(field._type, fields.Object):
            return field._type._obj_name
        if isinstance(field, fields.ListOfObjectsField):
            return field._type._element_type._type._obj_name
        return None

    def _build_tree(self, tree, obj_class):
        obj_name = obj_class.obj_name()
        if obj_name in tree:
            return

        obj_classes = base.NovaObjectRegistry.obj_classes()
        for name, field in obj_class.fields.items():
            sub_obj_name = self._get_object_field_name(field)
            if sub_obj_name:
                sub_obj_class = obj_classes[sub_obj_name][0]
                tree.setdefault(obj_name, {})
                tree[obj_name][sub_obj_name] = sub_obj_class.VERSION

    def test_relationships(self):
        tree = {}
        obj_classes = base.NovaObjectRegistry.obj_classes()
        for obj_name in obj_classes.keys():
            self._build_tree(tree, obj_classes[obj_name][0])

        stored = set([(x, str(y)) for x, y in object_relationships.items()])
        computed = set([(x, str(y)) for x, y in tree.items()])
        changed = stored.symmetric_difference(computed)
        expected = {}
        actual = {}
        for name, deps in changed:
            expected[name] = object_relationships.get(name)
            actual[name] = tree.get(name)
        self.assertEqual(expected, actual,
                         'Some objects have changed dependencies. '
                         'Please make sure to bump the versions of '
                         'parent objects and provide a rule in their '
                         'obj_make_compatible() routines to backlevel '
                         'the child object. Also remember to update versions '
                         'of child objects as necessary in the '
                         'object_relationships mapping used in this test.')

    def test_obj_make_compatible(self):
        # Iterate all object classes and verify that we can run
        # obj_make_compatible with every older version than current.
        # This doesn't actually test the data conversions, but it at least
        # makes sure the method doesn't blow up on something basic like
        # expecting the wrong version format.
        obj_classes = base.NovaObjectRegistry.obj_classes()
        for obj_name in obj_classes:
            obj_class = obj_classes[obj_name][0]
            version = utils.convert_version_to_tuple(obj_class.VERSION)
            for n in range(version[1]):
                test_version = '%d.%d' % (version[0], n)
                LOG.info('testing obj: %s version: %s' %
                         (obj_name, test_version))
                obj_class().obj_to_primitive(target_version=test_version)

    def _get_obj_to_test(self, obj_class):
        obj = obj_class()
        obj_classes = base.NovaObjectRegistry.obj_classes()
        for fname, ftype in obj.fields.items():
            if isinstance(ftype, fields.ObjectField):
                fobjname = ftype.AUTO_TYPE._obj_name
                fobjcls = obj_classes[fobjname][0]
                setattr(obj, fname, self._get_obj_to_test(fobjcls))
            elif isinstance(ftype, fields.ListOfObjectsField):
                # FIXME(danms): This will result in no tests for this
                # field type...
                setattr(obj, fname, [])
        return obj

    def _find_version_mapping(self, my_ver, versions):
        closest = None
        my_ver = utils.convert_version_to_tuple(my_ver)
        for _my, _child in versions:
            _my = utils.convert_version_to_tuple(_my)
            _child = utils.convert_version_to_tuple(_child)
            if _my == my_ver:
                return '%s.%s' % _child
            elif _my < my_ver:
                closest = _child
        if closest:
            return '%s.%s' % closest
        else:
            return None

    def _validate_object_fields(self, obj_class, primitive):
        for fname, ftype in obj_class.fields.items():
            if isinstance(ftype, fields.ObjectField):
                exp_vers = obj_class.obj_relationships[fname]
                exp_ver = self._find_version_mapping(
                    primitive['nova_object.version'], exp_vers)
                if exp_ver is None:
                    self.assertNotIn(fname, primitive['nova_object.data'])
                else:
                    child_p = primitive['nova_object.data'][fname]
                    self.assertEqual(exp_ver,
                                     child_p['nova_object.version'])

    def test_obj_make_compatible_with_data(self):
        # Iterate all object classes and verify that we can run
        # obj_make_compatible with every older version than current.
        # This doesn't actually test the data conversions, but it at least
        # makes sure the method doesn't blow up on something basic like
        # expecting the wrong version format.
        obj_classes = base.NovaObjectRegistry.obj_classes()
        for obj_name in obj_classes:
            obj_class = obj_classes[obj_name][0]
            if 'tests.unit' in obj_class.__module__:
                # NOTE(danms): Skip test objects. When we move to
                # oslo.versionedobjects, we won't have to do this
                continue
            version = utils.convert_version_to_tuple(obj_class.VERSION)
            for n in range(version[1]):
                test_version = '%d.%d' % (version[0], n)
                LOG.info('testing obj: %s version: %s' %
                         (obj_name, test_version))
                test_object = self._get_obj_to_test(obj_class)
                obj_p = test_object.obj_to_primitive(
                    target_version=test_version)
                self._validate_object_fields(obj_class, obj_p)

    def test_obj_relationships_in_order(self):
        # Iterate all object classes and verify that we can run
        # obj_make_compatible with every older version than current.
        # This doesn't actually test the data conversions, but it at least
        # makes sure the method doesn't blow up on something basic like
        # expecting the wrong version format.
        obj_classes = base.NovaObjectRegistry.obj_classes()
        for obj_name in obj_classes:
            obj_class = obj_classes[obj_name][0]
            for field, versions in obj_class.obj_relationships.items():
                last_my_version = (0, 0)
                last_child_version = (0, 0)
                for my_version, child_version in versions:
                    _my_version = utils.convert_version_to_tuple(my_version)
                    _ch_version = utils.convert_version_to_tuple(child_version)
                    self.assertTrue((last_my_version < _my_version
                                     and last_child_version <= _ch_version),
                                    'Object %s relationship '
                                    '%s->%s for field %s is out of order' % (
                                        obj_name, my_version, child_version,
                                        field))
                    last_my_version = _my_version
                    last_child_version = _ch_version


class TestObjEqualPrims(_BaseTestCase):

    def test_object_equal(self):
        obj1 = MyObj(foo=1, bar='goodbye')
        obj1.obj_reset_changes()
        obj2 = MyObj(foo=1, bar='goodbye')
        obj2.obj_reset_changes()
        obj2.bar = 'goodbye'
        # obj2 will be marked with field 'three' updated
        self.assertTrue(base.obj_equal_prims(obj1, obj2),
                        "Objects that differ only because one a is marked "
                        "as updated should be equal")

    def test_object_not_equal(self):
        obj1 = MyObj(foo=1, bar='goodbye')
        obj1.obj_reset_changes()
        obj2 = MyObj(foo=1, bar='hello')
        obj2.obj_reset_changes()
        self.assertFalse(base.obj_equal_prims(obj1, obj2),
                         "Objects that differ in any field "
                         "should not be equal")

    def test_object_ignore_equal(self):
        obj1 = MyObj(foo=1, bar='goodbye')
        obj1.obj_reset_changes()
        obj2 = MyObj(foo=1, bar='hello')
        obj2.obj_reset_changes()
        self.assertTrue(base.obj_equal_prims(obj1, obj2, ['bar']),
                        "Objects that only differ in an ignored field "
                        "should be equal")
