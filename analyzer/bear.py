# -*- coding: utf-8 -*-
#                     The LLVM Compiler Infrastructure
#
# This file is distributed under the University of Illinois Open Source
# License. See LICENSE.TXT for details.

import logging
import multiprocessing
import subprocess
import argparse
import json
import re
import os
import os.path
import sys
import glob
import pkg_resources
from analyzer.decorators import to_logging_level, trace
import analyzer.command as commands


if 'darwin' == sys.platform:
    ENVIRONMENTS = [("ENV_OUTPUT", "BEAR_OUTPUT"),
                    ("ENV_PRELOAD", "DYLD_INSERT_LIBRARIES"),
                    ("ENV_FLAT", "DYLD_FORCE_FLAT_NAMESPACE")]
else:
    ENVIRONMENTS = [("ENV_OUTPUT", "BEAR_OUTPUT"),
                    ("ENV_PRELOAD", "LD_PRELOAD")]


if sys.version_info.major >= 3 and sys.version_info.minor >= 2:
    from tempfile import TemporaryDirectory
else:
    class TemporaryDirectory(object):
        """ This function creates a temporary directory using mkdtemp() (the
        supplied arguments are passed directly to the underlying function).
        The resulting object can be used as a context manager. On completion
        of the context or destruction of the temporary directory object the
        newly created temporary directory and all its contents are removed
        from the filesystem. """
        def __init__(self, **kwargs):
            from tempfile import mkdtemp
            self.name = mkdtemp(*kwargs)

        def __enter__(self):
            return self.name

        def __exit__(self, _type, _value, _traceback):
            self.cleanup()

        def cleanup(self):
            from shutil import rmtree
            if self.name is not None:
                rmtree(self.name)


def main():
    """ Entry point for 'bear'.

    'bear' is a tool that generates a compilation database for clang tooling.

    The JSON compilation database is used in the clang project to provide
    information on how a single compilation unit is processed. With this,
    it is easy to re-run the compilation with alternate programs.

    The concept behind 'bear' is to execute the original build command and
    intercept the exec calls issued by the build tool. To achieve that,
    'bear' uses the LD_PRELOAD or DYLD_INSERT_LIBRARIES mechanisms provided by
    the dynamic linker. """
    multiprocessing.freeze_support()
    logging.basicConfig(format='bear: %(message)s')

    try:
        parser = create_command_line_parser()
        args = parser.parse_args()

        logging.getLogger().setLevel(to_logging_level(args.verbose))
        logging.debug(args)

        exit_code = 0
        with TemporaryDirectory(prefix='bear-') as tmpdir:
            exit_code = run_build(args.build, tmpdir)
            commands = collect(not args.filtering, tmpdir)
            with open(args.output, 'w+') as handle:
                json.dump(commands, handle, sort_keys=True, indent=4)
        return exit_code

    except Exception as exception:
        print(str(exception))
        return 127


@trace
def create_command_line_parser():
    """ Parse command line and return a dictionary of given values. """
    parser = argparse.ArgumentParser(
        prog='bear',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        '-o', '--output',
        metavar='<file>',
        dest='output',
        default="compile_commands.json",
        help="""Specifies the output directory for analyzer reports.
             Subdirectory will be created if default directory is targeted.""")
    parser.add_argument(
        '-v', '--verbose',
        action='count',
        default=0,
        help="""Enable verbose output from ‘bear’. A second and third '-v'
             increases verbosity.""")
    parser.add_argument(
        dest='build',
        nargs=argparse.REMAINDER,
        help="""Command to run.""")

    group2 = parser.add_argument_group('ADVANCED OPTIONS')
    group2.add_argument(
        '-n', '--disable-filter',
        dest='filtering',
        action='store_true',
        help="""Disable filter, unformated output.""")

    return parser


@trace
def run_build(command, destination):
    """ Runs the original build command.

    It sets the required environment variables and execute the given command.
    The exec calls will be logged by the 'libear' preloaded library. And
    placed into the output directory. """
    def get_ear_so_file():
        path = pkg_resources.get_distribution('beye').location
        candidates = glob.glob(os.path.join(path, 'ear*.so'))
        return candidates[0] if len(candidates) else None

    environment = dict(os.environ)
    for alias, key in ENVIRONMENTS:
        value = '1'
        if alias == 'ENV_PRELOAD':
            value = get_ear_so_file()
        elif alias == 'ENV_OUTPUT':
            value = destination
        environment.update({key: value})

    child = subprocess.Popen(command, env=environment, shell=True)
    child.wait()
    return child.returncode


@trace
def collect(filtering, destination):
    """ Collect the execution information from the output directory. """
    def parse(filename):
        """ Parse the file generated by the 'libear' preloaded library. """
        RS = chr(0x1e)
        US = chr(0x1f)
        with open(filename, 'r') as handler:
            content = handler.read()
            records = content.split(RS)
            return {'pid': records[0],
                    'ppid': records[1],
                    'function': records[2],
                    'directory': records[3],
                    'command': records[4].split(US)[:-1]}

    def general_filter(iterator):
        """ Filter out the non compiler invocations. """
        def known_compiler(command):
            patterns = [
                re.compile(r'^([^/]*/)*c(c|\+\+)$'),
                re.compile(r'^([^/]*/)*([^-]*-)*g(cc|\+\+)(-[34].[0-9])?$'),
                re.compile(r'^([^/]*/)*clang(\+\+)?(-[23].[0-9])?$'),
                re.compile(r'^([^/]*/)*llvm-g(cc|\+\+)$'),
            ]
            executable = command[0]
            for pattern in patterns:
                if pattern.match(executable):
                    return True
            return False

        def cancel_parameter(command):
            patterns = [
                re.compile(r'^-cc1$')
            ]
            for pattern in patterns:
                for arg in command[1:]:
                    if pattern.match(arg):
                        return True
            return False

        for record in iterator:
            command = record['command']
            if known_compiler(command) and not cancel_parameter(command):
                yield record

    def format_record(iterator):
        """ Generate the desired fields for compilation database entries. """
        def join_command(args):
            """ Create a single string from list.

            The major challange, which is not solved yet, to deal with white
            spaces. Which are used by the shell as separator.
            (Eg.: -D_KEY="Value with spaces") """
            return ' '.join(args)

        for record in iterator:
            atoms = commands.parse({'command': record['command']}, lambda x: x)
            if atoms['action'] == commands.Action.Compile:
                for filename in atoms['files']:
                    yield {'directory': record['directory'],
                           'command': join_command(record['command']),
                           'file': os.path.abspath(filename)}

    chain = lambda x: format_record(general_filter(x))

    generator = [parse(record)
                 for record
                 in glob.iglob(os.path.join(destination, 'cmd.*'))]
    return list(chain(generator)) if filtering else generator