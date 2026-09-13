from pathlib import Path
import unittest
import yaml


class WorkflowTests(unittest.TestCase):
    def test_schedule_dispatch_and_isolated_permissions(self):
        root = Path(__file__).resolve().parents[1]
        # BaseLoader preserves the YAML 1.2 Actions 'on' key under PyYAML 1.1.
        workflow = yaml.load((root / '.github/workflows/daily.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(workflow['on']['schedule'][0]['cron'], '0 21 * * *')
        self.assertEqual(workflow['on']['workflow_dispatch']['inputs']['dry_run']['type'], 'boolean')
        self.assertNotIn('pull_request_target', workflow['on'])
        jobs = workflow['jobs']
        self.assertEqual(jobs['generate']['permissions'], {'contents': 'read', 'copilot-requests': 'write'})
        self.assertEqual(jobs['publish']['permissions'], {'contents': 'write'})
        self.assertEqual(jobs['deploy']['permissions'], {'pages': 'write', 'id-token': 'write'})
        pipeline = next(s for s in jobs['generate']['steps'] if s.get('id') == 'pipeline')
        self.assertEqual(pipeline['env']['GITHUB_TOKEN'], '${{ github.token }}')
        self.assertEqual(pipeline['continue-on-error'], 'true')
        self.assertLess(int(pipeline['timeout-minutes']), int(jobs['generate']['timeout-minutes']))
        self.assertEqual(jobs['deploy']['needs'], ['generate', 'publish'])
        self.assertEqual(jobs['publish']['if'], '${{ !inputs.dry_run }}')
        self.assertEqual(workflow['concurrency']['cancel-in-progress'], 'false')


if __name__ == '__main__':
    unittest.main()
