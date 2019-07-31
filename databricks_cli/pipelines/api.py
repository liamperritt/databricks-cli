# Databricks CLI
# Copyright 2017 Databricks, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"), except
# that the use of services to which certain application programming
# interfaces (each, an "API") connect requires that the user first obtain
# a license for the use of the APIs from Databricks, Inc. ("Databricks"),
# by creating an account at www.databricks.com and agreeing to either (a)
# the Community Edition Terms of Service, (b) the Databricks Terms of
# Service, or (c) another written agreement between Licensee and Databricks
# for the use of the APIs.
#
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from hashlib import sha1
import os

from six.moves import urllib

from databricks_cli.sdk import DeltaPipelinesService
from databricks_cli.dbfs.api import DbfsApi
from databricks_cli.dbfs.dbfs_path import DbfsPath

# These imports are specific to the credentials part
from databricks_cli.configure.config import get_profile_from_context
from databricks_cli.configure.provider import get_config, ProfileConfigProvider
from databricks_cli.utils import InvalidConfigurationError

BUFFER_SIZE = 1024 * 64
base_pipelines_dir = 'dbfs:/pipelines/code'
supported_lib_types = {'jar', 'whl'}


class PipelinesApi(object):
    def __init__(self, api_client):
        self.client = DeltaPipelinesService(api_client)
        self.dbfs_client = DbfsApi(api_client)

    def deploy(self, spec, headers=None):
        lib_objects = LibraryObject.from_json(spec.get('libraries', []))
        local_lib_objects, external_lib_objects = \
            self._identify_local_libraries(lib_objects)

        spec['libraries'] = LibraryObject.to_json(external_lib_objects +
                                                  self._upload_local_libraries(local_lib_objects))
        spec['credentials'] = self._get_credentials_for_request()
        self.client.client.perform_query('PUT',
                                         '/pipelines/{}'.format(spec['id']),
                                         data=spec,
                                         headers=headers)

    def delete(self, pipeline_id, headers=None):
        self.client.delete(pipeline_id, self._get_credentials_for_request(), headers)

    @staticmethod
    def _identify_local_libraries(lib_objects):
        """
        Partitions the given set of libraries into local and those already present in dbfs/s3 etc.
        Local libraries are (currently) jar files with a file scheme or no scheme at all.
        All other libraries should be present in a supported external source.
        :param lib_objects: List[LibraryObject]
        :return: List[List[LibraryObject], List[LibraryObject]] ([Local, External])
        """
        local_lib_objects, external_lib_objects = [], []
        for lib_object in lib_objects:
            parsed_uri = urllib.parse.urlparse(lib_object.path)
            if lib_object.lib_type in supported_lib_types and parsed_uri.scheme == '':
                local_lib_objects.append(lib_object)
            elif lib_object.lib_type in supported_lib_types and parsed_uri.scheme.lower() == 'file':
                # exactly 1 or 3
                if parsed_uri.path.startswith('//') or parsed_uri.netloc != '':
                    raise RuntimeError('invalid file uri scheme, '
                                       'did you mean to use file:/ or file:///')
                local_lib_objects.append(LibraryObject(lib_object.lib_type, parsed_uri.path))
            else:
                external_lib_objects.append(lib_object)
        return local_lib_objects, external_lib_objects

    def _upload_local_libraries(self, local_lib_objects):
        remote_lib_objects = [LibraryObject(llo.lib_type, self._get_hashed_path(llo.path))
                              for llo in local_lib_objects]

        transformed_remote_lib_objects = [LibraryObject(rlo.lib_type, DbfsPath(rlo.path))
                                          for rlo in remote_lib_objects]
        upload_files = [llo_tuple for llo_tuple in
                        zip(local_lib_objects, transformed_remote_lib_objects)
                        if not self.dbfs_client.file_exists(llo_tuple[1].path)]

        for llo, rlo in upload_files:
            self.dbfs_client.put_file(llo.path, rlo.path, False)

        return remote_lib_objects

    @staticmethod
    def _get_hashed_path(path):
        """
        Finds the corresponding dbfs file path for the file located at the supplied path by
        calculating its hash using SHA1.
        :param path: Local File Path
        :return: Remote Path (pipeline_base_dir + file_hash (dot) file_extension)
        """
        hash_buffer = sha1()
        with open(path, 'rb') as f:
            while True:
                data = f.read(BUFFER_SIZE)
                if not data:
                    break
                hash_buffer.update(data)

        file_hash = hash_buffer.hexdigest()
        # splitext includes the period in the extension
        path = '{}/{}{}'.format(base_pipelines_dir, file_hash, os.path.splitext(path)[1])
        return path

    @staticmethod
    def _get_credentials_for_request():
        """
        Only required while the deploy/delete APIs require credentials in the body as well
        as the header. Once the API requirement is relaxed, we can remove this function"
        """
        profile = get_profile_from_context()
        if profile:
            config = ProfileConfigProvider.get_config(profile)
        else:
            config = get_config()
        if not config or not config.is_valid:
            raise InvalidConfigurationError.for_profile(profile)

        if config.is_valid_with_token:
            return {'token': config.token}
        else:
            return {'user': config.username, 'password': config.password}


class LibraryObject(object):
    def __init__(self, lib_type, lib_path):
        self.path = lib_path
        self.lib_type = lib_type

    @classmethod
    def from_json(cls, libraries):
        """
        Serialize Libraries into LibraryObjects
        :param libraries: List[Dictionary{String, String}]
        :return: List[LibraryObject]
        """
        lib_objects = []
        for library in libraries:
            for lib_type, path in library.items():
                lib_objects.append(LibraryObject(lib_type, path))
        return lib_objects

    @classmethod
    def to_json(cls, lib_objects):
        """
        Deserialize LibraryObjects
        :param lib_objects: List[LibraryObject]
        :return: List[Dictionary{String, String}]
        """
        libraries = []
        for lib_object in lib_objects:
            libraries.append({lib_object.lib_type: lib_object.path})
        return libraries

    def __eq__(self, other):
        if not isinstance(other, LibraryObject):
            return NotImplemented
        return self.path == other.path and self.lib_type == other.lib_type
