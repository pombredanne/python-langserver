from os import path as filepath

import jedi

import opentracing
from typing import List

from .fs import RemoteFileSystem


class Module:
    def __init__(self, name, path, is_package=False):
        self.name = name
        self.path = path
        self.is_package = is_package

    def __repr__(self):
        return "PythonModule({}, {})".format(self.name, self.path)


class DummyFile:
    def __init__(self, contents):
        self.contents = contents

    def read(self):
        return self.contents

    def close(self):
        pass


class RemoteJedi:
    def __init__(self, fs, workspace, root_path):
        self.fs = fs
        self.workspace = workspace
        self.root_path = root_path

    def workspace_modules(self, path, parent_span) -> List[Module]:
        """Return a set of all python modules found within a given path."""

        with opentracing.start_child_span(
                parent_span, "workspace_modules") as workspace_modules_span:
            workspace_modules_span.set_tag("path", path)

            dir = self.fs.listdir(path, workspace_modules_span)
            modules = []
            for e in dir:
                if e.is_dir:
                    subpath = filepath.join(path, e.name)
                    subdir = self.fs.listdir(subpath, workspace_modules_span)
                    if any([s.name == "__init__.py" for s in subdir]):
                        modules.append(
                            Module(e.name,
                                   filepath.join(subpath, "__init__.py"),
                                   True))
                else:
                    name, ext = filepath.splitext(e.name)
                    if ext == ".py":
                        if name == "__init__":
                            name = filepath.basename(path)
                            modules.append(
                                Module(name, filepath.join(path, e.name),
                                       True))
                        else:
                            modules.append(
                                Module(name, filepath.join(path, e.name)))

            return modules

    def new_script(self, *args, **kwargs):
        """Return an initialized Jedi API Script object."""
        if "parent_span" in kwargs:
            parent_span = kwargs.get("parent_span")
            del kwargs["parent_span"]
        else:
            parent_span = opentracing.tracer.start_span("new_script_parent")

        with opentracing.start_child_span(parent_span,
                                          "new_script") as new_script_span:
            path = kwargs.get("path")
            new_script_span.set_tag("path", path)
            return self._new_script_impl(new_script_span, *args, **kwargs)

    def _new_script_impl(self, parent_span, *args, **kwargs):
        path = kwargs.get("path")

        trace = False
        if 'trace' in kwargs:
            trace = True
            del kwargs['trace']

        def find_module_remote(string, dir=None, fullname=None):
            """A swap-in replacement for Jedi's find module function that uses the
            remote fs to resolve module imports."""
            with opentracing.start_child_span(
                    parent_span,
                    "find_module_remote_callback") as find_module_span:
                if trace:
                    print("find_module_remote", string, dir, fullname)

                the_module = None

                # default behavior is to search for built-ins first
                if fullname:
                    the_module = self.workspace.find_stdlib_module(fullname)

                # after searching for built-ins, search the current project
                if not the_module:
                    the_module = self.workspace.find_project_module(fullname)

                # finally, search 3rd party dependencies
                if not the_module:
                    the_module = self.workspace.find_external_module(fullname)

                if not the_module:
                    raise ImportError('Module "{}" not found in {}', string, dir)

                is_package = the_module.is_package
                module_file = DummyFile(self.workspace.open_module_file(the_module, find_module_span))
                module_path = filepath.dirname(the_module.path) if is_package else the_module.path
                return module_file, module_path, is_package

        # TODO: update this to use the workspace's module indices
        def list_modules() -> List[str]:
            if trace:
                print("list_modules")
            modules = [
                f for f in self.fs.walk(self.root_path)
                if f.lower().endswith(".py")
            ]
            return modules

        def load_source(path) -> str:
            with opentracing.start_child_span(
                    parent_span, "load_source_callback") as load_source_span:
                load_source_span.set_tag("path", path)
                if trace:
                    print("load_source", path)
                result = self.fs.open(path, load_source_span)
                return result

        # TODO(keegan) It shouldn't matter if we are using a remote fs or not. Consider other ways to hook into the import system.
        if isinstance(self.fs, RemoteFileSystem):
            kwargs.update(
                find_module=find_module_remote,
                list_modules=list_modules,
                load_source=load_source, )

        return jedi.api.Script(*args, **kwargs)
