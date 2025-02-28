import logging
import os
import pathlib
import datetime
from functools import wraps
from typing import Dict, Optional, Union, List

import git
import toml
from sortedcontainers import SortedDict


from libbs.artifacts import (
    Comment,
    Enum,
    Function,
    FunctionHeader,
    GlobalVariable,
    Patch,
    StackVariable,
    Struct,
)
from libbs.artifacts import TomlHexEncoder
from binsync import __version__ as BS_VERS
from binsync.core.errors import MetadataNotFoundError


l = logging.getLogger(__name__)


class ArtifactType:
    UNSET = None
    FUNCTION = "function"
    STRUCT = "struct"
    PATCH = "patch"
    COMMENT = "comment"
    GLOBAL_VAR = "global variable"
    ENUM = "enum"


#
# Helper Funcs
#

def update_dirty_flag(f):
    @wraps(f)
    def _update_dirty_flag(self, *args, **kwargs):
        r = f(self, *args, **kwargs)
        if r is True:
            self._dirty = True
        return r

    return _update_dirty_flag


def update_last_change(f):
    @wraps(f)
    def _update_last_change(self, *args, **kwargs):
        should_set = kwargs.pop('set_last_change', True)
        from_user = kwargs.pop('from_user', None)
        artifact = args[0]

        # make a function if one does not exist
        if isinstance(artifact, (FunctionHeader, StackVariable)):
            func = self.get_or_make_function(artifact.addr)

        if not should_set:
            from_user_msg = f" from {from_user}" if from_user else ""
            self.last_commit_msg = f"Merged in {artifact}{from_user_msg}"
            return f(self, *args, **kwargs)

        self.last_commit_msg = f"Updated {artifact}"
        artifact.last_change = datetime.datetime.now(tz=datetime.timezone.utc)

        # Comment
        if isinstance(artifact, Comment):
            artifact_loc = artifact.addr
            artifact_type = ArtifactType.COMMENT
            # update function its in, if it's in a function
            func = self.find_func_for_addr(artifact.addr)
            if func:
                func.last_change = artifact.last_change

        # Stack Var
        elif isinstance(artifact, StackVariable):
            artifact_loc = artifact.addr
            artifact_type = ArtifactType.FUNCTION
            func.last_change = artifact.last_change

        elif isinstance(artifact, Function):
            artifact_loc = artifact.addr
            artifact_type = ArtifactType.FUNCTION

        # Function Header
        elif isinstance(artifact, FunctionHeader):
            artifact_loc = artifact.addr
            artifact_type = ArtifactType.FUNCTION
            func.last_change = artifact.last_change

        # Patch
        elif isinstance(artifact, Patch):
            artifact_loc = artifact.offset
            artifact_type = ArtifactType.PATCH

        # Struct
        elif isinstance(artifact, Struct):
            artifact_loc = artifact.name
            artifact_type = ArtifactType.STRUCT

        # Global Var
        elif isinstance(artifact, GlobalVariable):
            artifact_loc = artifact.addr
            artifact_type = ArtifactType.GLOBAL_VAR

        # Enum
        elif isinstance(artifact, Enum):
            artifact_loc = artifact.name
            artifact_type = ArtifactType.ENUM

        else:
            raise Exception("Undefined Artifact Type!")

        self.last_push_artifact = artifact_loc
        self.last_push_time = artifact.last_change
        self.last_push_artifact_type = artifact_type

        return f(self, *args, **kwargs)

    return _update_last_change


def list_files_in_dir(src: Union[pathlib.Path, git.Tree], dir_name, client=None) -> List[str]:
    if client and isinstance(src, git.Tree):
        files = client.list_files_in_tree(src)
        return [name for name in files if name.startswith(dir_name)]

    # load from filesystem
    if not src:
        src = pathlib.Path("")

    if not src.joinpath(pathlib.Path(dir_name)).exists():
        return []

    dir_name_path = pathlib.Path(dir_name)
    dir_path = src.joinpath(dir_name)
    return [
        str(dir_name_path.joinpath(pathlib.Path(name))) for name in os.listdir(dir_path)
    ]


def load_toml_from_file(src: Union[pathlib.Path, git.Tree], filename, client=None):
    if client and isinstance(src, git.Tree):
        file_data = client.load_file_from_tree(src, filename)
    else:
        if not src:
            src = pathlib.Path("")

        src = src.joinpath(filename)
        if not src.exists():
            file_data = None
        else:
            with open(src, "r") as fp:
                file_data = fp.read()

    return toml.loads(file_data) if file_data is not None else file_data


#
# State Defn & Operators
#

class State:
    """
    The state.

    :ivar str user:     Name of the user.
    :ivar int version:  Version of the state, starting from 0.
    """

    def __init__(self, user: str, version: str = None, client=None, last_push_time=None, last_commit_msg=None, dirty=True):
        # metadata info
        self.user = user  # type: str
        self.version = version or str(BS_VERS)
        self.last_push_artifact = -1
        self.last_push_artifact_type = -1
        self.last_push_time = last_push_time or datetime.datetime.now(tz=datetime.timezone.utc)
        self.last_commit_msg = last_commit_msg

        # the client
        self.client = client  # type: Optional[Client]

        # data
        self.functions: Dict[int, Function] = {}
        self.comments: Dict[int, Comment] = {}
        self.structs: Dict[str, Struct] = {}
        self.patches: Dict[int, Patch] = SortedDict()
        self.global_vars: Dict[int, GlobalVariable] = {}
        self.enums: Dict[str, Enum] = {}

        # state is dirty on creation (metadata)
        self._dirty = dirty  # type: bool

    def __eq__(self, other):
        if isinstance(other, State):
            return other.functions == self.functions \
                   and other.comments == self.comments \
                   and other.structs == self.structs \
                   and other.patches == self.patches \
                   and other.global_vars == self.global_vars \
                   and other.enums == self.enums
        return False

    def copy(self):
        state = State(self.user, version=self.version, client=self.client, last_push_time=self.last_push_time, last_commit_msg=self.last_commit_msg, dirty=self._dirty)
        artifacts = ["functions", "comments", "structs", "patches", "global_vars", "enums"]
        for artifact in artifacts:
            setattr(
                state,
                artifact,
                {k: v.copy() for k, v in getattr(self, artifact).items()}
            )

        return state

    def __str__(self):
        return f"<State: {self.user} " \
               f"funcs={len(self.functions)} " \
               f"cmts={len(self.comments)} " \
               f"globals={len(self.structs) + len(self.global_vars) + len(self.enums)}" \
               f">"

    def __repr__(self):
        return self.__str__()

    @property
    def dirty(self):
        return self._dirty

    def _dump_data(self, dst: Union[pathlib.Path, git.IndexFile], filename, data):
        # dump using Git files
        if self.client and isinstance(dst, git.IndexFile):
            self.client.add_data(dst, filename, data)
            return

        # dump using filesystem
        if not dst:
            dst = pathlib.Path("")

        out_path = dst.joinpath(filename)
        pathlib.Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "wb") as fp:
            fp.write(data)

    def dump_metadata(self, dst: Union[pathlib.Path, git.IndexFile]):
        d = {
            "user": self.user,
            "version": self.version,
            "last_push_time": self.last_push_time,
            "last_push_artifact": self.last_push_artifact,
            "last_push_artifact_type": self.last_push_artifact_type,
            "last_commit_msg": self.last_commit_msg,
        }
        self._dump_data(dst, 'metadata.toml', toml.dumps(d, encoder=TomlHexEncoder()).encode())

    def dump(self, dst: Union[pathlib.Path, git.IndexFile]):
        if isinstance(dst, str):
            dst = pathlib.Path(dst)

        # dump metadata
        self.dump_metadata(dst)

        # dump functions, one file per function in ./functions/
        for addr, func in self.functions.items():
            path = pathlib.Path('functions').joinpath("%08x.toml" % addr)
            self._dump_data(dst, path, func.dump().encode())

        # dump structs, one file per struct in ./structs/
        for s_name, struct in self.structs.items():
            path = pathlib.Path('structs').joinpath(f"{s_name}.toml")
            self._dump_data(dst, path, struct.dump().encode())

        # dump comments
        self._dump_data(dst, 'comments.toml', toml.dumps(Comment.dump_many(self.comments), encoder=TomlHexEncoder()).encode())

        # dump patches
        self._dump_data(dst, 'patches.toml', toml.dumps(Patch.dump_many(self.patches), encoder=TomlHexEncoder()).encode())

        # dump global vars
        self._dump_data(dst, 'global_vars.toml', toml.dumps(GlobalVariable.dump_many(self.global_vars), encoder=TomlHexEncoder()).encode())

        # dump enums
        self._dump_data(dst, 'enums.toml', toml.dumps(Enum.dump_many(self.enums), encoder=TomlHexEncoder()).encode())

    @classmethod
    def parse(cls, src: Union[pathlib.Path, git.Tree], client=None):
        if isinstance(src, str):
            src = pathlib.Path(src)

        state = cls(None, client=client)

        # load metadata
        metadata = load_toml_from_file(src, "metadata.toml", client=client)
        if metadata is None:
            # metadata is not found
            raise MetadataNotFoundError()
        state.user = metadata["user"]
        state.version = metadata["version"]
        state.last_push_time = metadata.get("last_push_time", None)

        # load functions
        function_files = list_files_in_dir(src, "functions", client=client)
        for func_file in function_files:
            func_toml = load_toml_from_file(src, func_file, client=client)
            func = Function.load(func_toml)
            state.functions[func.addr] = func

        # load comments
        comments_toml = load_toml_from_file(src, "comments.toml", client=client)
        comments = {}
        if comments_toml:
            for comment in Comment.load_many(comments_toml):
                comments[comment.addr] = comment
        state.comments = comments

        # load patches
        patches_toml = load_toml_from_file(src, "patches.toml", client=client)
        patches = {}
        if patches_toml:
            for patch in Patch.load_many(patches_toml):
                patches[patch.offset] = patch
        state.patches = SortedDict(patches)

        # load global_vars
        global_vars_toml = load_toml_from_file(src, "global_vars.toml", client=client)
        global_vars = {}
        if global_vars_toml:
            for global_var in GlobalVariable.load_many(global_vars_toml):
                global_vars[global_var.addr] = global_var
        state.global_vars = SortedDict(global_vars)

        # load enums
        enums_toml = load_toml_from_file(src, "enums.toml", client=client)
        state.enums = {
            enum.name: enum for enum in Enum.load_many(enums_toml)
        } if enums_toml else {}

        # load structs
        struct_files = list_files_in_dir(src, "structs", client=client)
        for struct_file in struct_files:
            struct_toml = load_toml_from_file(src, struct_file, client=client)
            struct = Struct.load(struct_toml)
            state.structs[struct.name] = struct

        # clear the dirty bit
        state._dirty = False
        return state

    #
    # Setters
    #

    @update_dirty_flag
    @update_last_change
    def set_function(self, function: Function, set_last_change=True, **kwargs):
        if function.addr in self.functions and self.functions[function.addr] == function:
            return False

        self.functions[function.addr] = function
        return True

    @update_dirty_flag
    @update_last_change
    def set_function_header(self, func_header: FunctionHeader, set_last_change=True, **kwargs):
        if func_header.addr in self.functions and self.functions[func_header.addr] == func_header:
            return False

        self.functions[func_header.addr].header = func_header
        return True

    @update_dirty_flag
    @update_last_change
    def set_comment(self, comment: Comment, append=False, set_last_change=True, **kwargs):
        if not comment:
            return False

        try:
            old_cmt = self.comments[comment.addr]
        except KeyError:
            old_cmt = None

        if old_cmt != comment:
            if old_cmt is not None and append:
                comment.comment = old_cmt.comment + "\n" + comment.comment
                if set_last_change:
                    comment.last_change = comment.last_change or old_cmt.last_change

            self.comments[comment.addr] = comment
            return True

        return False

    @update_dirty_flag
    @update_last_change
    def set_patch(self, patch, addr, set_last_change=True, **kwargs):
        if not patch:
            return False

        try:
            old_patch = self.patches[addr]
        except KeyError:
            old_patch = None

        if old_patch != patch:
            self.patches[addr] = patch
            return True

        return False

    @update_dirty_flag
    @update_last_change
    def set_stack_variable(self, variable: StackVariable, set_last_change=True, **kwargs):
        if not variable:
            return False

        func = self.get_function(variable.addr)
        if not func:
            return False

        try:
            old_var = func.stack_vars[variable.offset]
        except KeyError:
            old_var = None

        if old_var != variable:
            func.stack_vars[variable.offset] = variable
            return True

        return False

    @update_dirty_flag
    @update_last_change
    def set_struct(self, struct: Struct, old_name=None, set_last_change=True, **kwargs):
        """
        Sets a struct in the current state. If old_name is not defined (None), then
        this indicates that the struct has not changed names. In that case, simply overwrite the
        internal representation of the struct.

        If the old_name is defined, than a struct has changed names. In that case, delete
        the internal struct data and delete the related .toml file.

        @param struct:
        @param old_name:
        @param set_last_change:
        @return:
        """
        if struct.name in self.structs \
                and self.structs[struct.name] == struct:
            # no updated is required
            return False

        # delete old struct only when we know what it is
        if old_name is not None:
            try:
                del self.structs[old_name]
                #remove_data(self.client.repo.index, os.path.join('structs', f'{old_name}.toml'))
            except KeyError:
                pass

        # set the new struct
        if struct.name is not None:
            self.structs[struct.name] = struct
            return True

        return False

    @update_dirty_flag
    @update_last_change
    def set_global_var(self, gloabl_var: GlobalVariable, set_last_change=True, **kwargs):
        try:
            old_gvar = self.global_vars[gloabl_var.addr]
        except KeyError:
            old_gvar = None

        if old_gvar != gloabl_var:
            self.global_vars[gloabl_var.addr] = gloabl_var
            return True

        return False

    @update_dirty_flag
    @update_last_change
    def set_enum(self, enum: Enum, set_last_change=True, **kwargs):
        try:
            old_enum = self.enums[enum.name]
        except KeyError:
            old_enum = None

        if old_enum != enum:
            self.enums[enum.name] = enum
            return True

        return False

    #
    # Getters
    #

    def get_or_make_function(self, addr) -> Function:
        try:
            func = self.functions[addr]
        except KeyError:
            self.functions[addr] = Function(addr, 0)
            func = self.functions[addr]

        return func

    def get_function(self, addr) -> Function:
        return self.functions.get(addr, None)

    def get_functions(self) -> Dict[int, Function]:
        return self.functions

    def get_function_header(self, addr) -> Optional[FunctionHeader]:
        func = self.get_function(addr)
        if not func:
            return None

        return func.header

    def get_function_headers(self) -> Dict[int, FunctionHeader]:
        return {
            addr: func.header
            for addr, func in self.functions.items() if func.header
        }

    def get_comment(self, addr) -> Comment:
        try:
            cmt = self.comments[addr]
        except KeyError:
            cmt = None

        return cmt

    def get_func_comments(self, func_addr) -> Dict[int, Comment]:
        try:
            func = self.functions[func_addr]
        except KeyError:
            return {}

        return {
            addr: cmt for addr, cmt in self.comments.items()
            if func.addr <= addr <= func.addr + func.size
        }

    def get_patch(self, addr) -> Patch:
        try:
            patch = self.patches[addr]
        except KeyError:
            patch = None

        return patch

    def get_patches(self) -> Dict[int, Patch]:
        return self.patches

    def get_stack_variable(self, func_addr, offset) -> Optional[StackVariable]:
        func = self.get_function(func_addr)
        if not func:
            return None

        try:
            stack_var = func.stack_vars[offset]
        except KeyError:
            stack_var = None

        return stack_var

    def get_stack_variables(self, func_addr) -> Dict[int, StackVariable]:
        func = self.get_function(func_addr)
        if not func:
            return {}

        return func.stack_vars

    def get_struct(self, struct_name) -> Optional[Struct]:
        try:
            struct = self.structs[struct_name]
        except KeyError:
            struct = None

        return struct

    def get_structs(self) -> Dict[str, Struct]:
        return self.structs

    def get_global_var(self, addr):
        try:
            gvar = self.global_vars[addr]
        except KeyError:
            gvar = None

        return gvar

    def get_global_vars(self):
        return self.global_vars

    def get_enum(self, name):
        try:
            enum = self.enums[name]
        except KeyError:
            enum = None

        return enum

    def get_enums(self):
        return self.enums

    def get_last_push_for_artifact_type(self, artifact_type):
        last_change = -1
        artifact = None

        if artifact_type == ArtifactType.FUNCTION:
            for function in self.functions.values():
                if function.last_change > last_change:
                    last_change = function.last_change
                    artifact = function.addr
        elif artifact_type == ArtifactType.STRUCT:
            for struct in self.structs.values():
                if struct.last_change > last_change:
                    last_change = struct.last_change
                    artifact = struct.name
        elif artifact_type == ArtifactType.PATCH:
            for patch in self.patches.values():
                if patch.last_change > last_change:
                    last_change = patch.last_change
                    artifact = patch.offset

        return tuple((artifact, last_change))

    #
    # Utils
    #

    def find_func_for_addr(self, search_addr):
        for func_addr, func in self.functions.items():
            if func.addr <= search_addr < (func.addr + func.size):
                return func
        else:
            return None
