# coding=utf-8
# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

import json
import os
from collections import defaultdict

from pants.backend.jvm.subsystems.dependency_context import DependencyContext
from pants.backend.jvm.subsystems.zinc import Zinc
from pants.backend.jvm.tasks.nailgun_task import NailgunTask
from pants.base.build_environment import get_buildroot
from pants.base.workunit import WorkUnitLabel
from pants.goal.products import MultipleRootedProducts
from pants.java.jar.jar_dependency import JarDependency
from pants.util.memo import memoized_property


class AnalysisExtraction(NailgunTask):
  """A task that handles extracting product and dependency information from zinc analysis."""

  # The output JSON created by this task is not localized, but is used infrequently enough
  # that re-computing it from the zinc analysis (which _is_ cached) when necessary is fine.
  create_target_dirs = True

  _TOOL_NAME = 'zinc-extractor'

  @classmethod
  def subsystem_dependencies(cls):
    return super(AnalysisExtraction, cls).subsystem_dependencies() + (DependencyContext, Zinc)

  @classmethod
  def register_options(cls, register):
    super(AnalysisExtraction, cls).register_options(register)

    cls.register_jvm_tool(register,
                          cls._TOOL_NAME,
                          classpath=[
                            JarDependency(org='org.pantsbuild',
                                          name='zinc-extractor_2.11',
                                          rev='stuhood-zinc-1.0.0-X16-18')
                          ])

  @classmethod
  def prepare(cls, options, round_manager):
    super(AnalysisExtraction, cls).prepare(options, round_manager)
    round_manager.require_data('zinc_analysis')
    round_manager.require_data('runtime_classpath')

  @classmethod
  def product_types(cls):
    return ['classes_by_source', 'product_deps_by_src']

  def _create_products_if_should_run(self):
    """If this task should run, initialize empty products that it will populate.

    Returns true if the task should run.
    """

    should_run = False
    if self.context.products.is_required_data('classes_by_source'):
      should_run = True
      make_products = lambda: defaultdict(MultipleRootedProducts)
      self.context.products.safe_create_data('classes_by_source', make_products)

    if self.context.products.is_required_data('product_deps_by_src'):
      should_run = True
      self.context.products.safe_create_data('product_deps_by_src', dict)
    return should_run

  def _summary_json_file(self, vt):
    return os.path.join(vt.results_dir, 'summary.json')

  @memoized_property
  def _analysis_by_runtime_entry(self):
    zinc_analysis = self.context.products.get_data('zinc_analysis')
    return {cp_entry: analysis_file for _, cp_entry, analysis_file in zinc_analysis.values()}

  def execute(self):
    # If none of our computed products are necessary, return immediately.
    if not self._create_products_if_should_run():
      return

    zinc_analysis = self.context.products.get_data('zinc_analysis')
    classpath_product = self.context.products.get_data('runtime_classpath')
    classes_by_source = self.context.products.get_data('classes_by_source')
    product_deps_by_src = self.context.products.get_data('product_deps_by_src')

    fingerprint_strategy = DependencyContext.global_instance().create_fingerprint_strategy(
        classpath_product)

    targets = zinc_analysis.keys()
    with self.invalidated(targets,
                          fingerprint_strategy=fingerprint_strategy,
                          invalidate_dependents=True) as invalidation_check:
      # Extract and parse products for any relevant targets.
      for vt in invalidation_check.all_vts:
        summary_json_file = self._summary_json_file(vt)
        cp_entry, _, analysis_file = zinc_analysis[vt.target]
        if not vt.valid:
          self._extract_analysis(vt.target, analysis_file, summary_json_file)
        self._register_products(vt.target,
                                cp_entry,
                                summary_json_file,
                                classes_by_source,
                                product_deps_by_src)

  def _extract_analysis(self, target, analysis_file, summary_json_file):
    target_classpath = Zinc.global_instance().compile_classpath(self.context.products,
                                                                'runtime_classpath',
                                                                target)
    analysis_by_cp_entry = self._analysis_by_runtime_entry
    upstream_analysis = list(self._upstream_analysis(target_classpath, analysis_by_cp_entry))
    args = [
        '-summary-json', summary_json_file,
        '-analysis-cache', analysis_file,
        '-classpath', ':'.join(target_classpath),
        '-analysis-map', ','.join('{}:{}'.format(k, v) for k, v in upstream_analysis),
      ]
    args.extend(Zinc.global_instance().rebase_map_args)

    result = self.runjava(classpath=self.tool_classpath(self._TOOL_NAME),
                          main='org.pantsbuild.zinc.extractor.Main',
                          args=args,
                          workunit_name=self._TOOL_NAME,
                          workunit_labels=[WorkUnitLabel.MULTITOOL])
    if result != 0:
      raise TaskError('Failed to parse analysis for {}'.format(target.address.spec),
                      exit_code=result)

  def _upstream_analysis(self, target_classpath, analysis_by_cp_entry):
    for entry in target_classpath:
      analysis_file = analysis_by_cp_entry.get(entry)
      if analysis_file is not None:
        yield entry, analysis_file

  def _register_products(self,
                         target,
                         target_cp_entry,
                         summary_json_file,
                         classes_by_source,
                         product_deps_by_src):
    summary_json = self._parse_summary_json(summary_json_file)

    # Register a mapping between sources and classfiles (if requested).
    if classes_by_source is not None:
      buildroot = get_buildroot()
      for abs_src, classes in summary_json['products'].items():
        source = os.path.relpath(abs_src, buildroot)
        classes_by_source[source].add_abs_paths(target_cp_entry, classes)

    # Register classfile product dependencies (if requested).
    if product_deps_by_src is not None:
      # TODO: This is supposed to be per-source, but currently the product is not exposed
      # that way on the scala side.
      product_deps_by_src[target] = summary_json['dependencies']

  def _parse_summary_json(self, summary_json_file):
    with open(summary_json_file) as f:
      return json.load(f, encoding='utf-8')
