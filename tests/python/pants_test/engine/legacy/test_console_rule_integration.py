# coding=utf-8
# Copyright 2018 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

from pants_test.pants_run_integration_test import PantsRunIntegrationTest, ensure_daemon


class TestConsoleRuleIntegration(PantsRunIntegrationTest):

  # TODO: Running this command a second time with the daemon will result in no output because
  # of product caching.
  @ensure_daemon
  def test_v2_list(self):
    result = self.do_command(
      '--no-v1',
      '--v2',
      'list',
      '::',
      success=True
    )

    output_lines = result.stdout_data.splitlines()
    self.assertGreater(len(output_lines), 1000)
    self.assertIn('3rdparty/python:psutil', output_lines)
