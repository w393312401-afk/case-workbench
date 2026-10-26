"""Voice runtime phases describe real work and clear on completion or failure."""
import copy
import unittest
from unittest.mock import patch
from uuid import uuid4

from elevenlabs_client import VoiceError
from test_production import WorkbenchTests


class VoiceProgressTests(WorkbenchTests):
    def test_phase_receipts_follow_provider_alignment_and_save(self):
        job = self.job(self.adopt('One kit, replacing this jig.\nOut goes the jig.\nFull 202H kit\'s below.'))
        stages = []
        speech = self.bench.voices.speech
        timing = self.bench._timing

        def record_speech(*args,**kwargs):
            runtime = self.bench.production_catalog()['jobs'][0]['voicing_live']
            self.assertTrue(runtime['started_at'])
            stages.append(runtime['stage'])
            return speech(*args,**kwargs)

        def record_timing(*args,**kwargs):
            stages.append(copy.deepcopy(self.bench._voice_live()[job['id']])['stage'])
            return timing(*args,**kwargs)

        def record_save(*args,**kwargs):
            stages.append(self.bench._voice_live()[job['id']]['stage'])
            return False,None

        with patch.object(self.bench.voices,'speech',side_effect=record_speech),patch.object(self.bench,'_timing',side_effect=record_timing),patch('workbench_production.convert_wav',side_effect=record_save):
            self.bench.generate_take(job['id'],{'request_id':uuid4().hex,'voice_id':'VOICE123456789'})
        self.assertEqual(stages,['request','alignment','saving'])
        catalog = self.bench.production_catalog()['jobs'][0]
        self.assertFalse(catalog['voicing'])
        self.assertIsNone(catalog['voicing_live'])

    def test_runtime_clears_on_alignment_failure(self):
        job = self.job(self.adopt('One kit, replacing this jig.\nOut goes the jig.\nFull 202H kit\'s below.'))
        with patch.object(self.bench,'_timing',side_effect=VoiceError(503,'alignment unavailable')):
            with self.assertRaises(VoiceError):
                self.bench.generate_take(job['id'],{'request_id':uuid4().hex,'voice_id':'VOICE123456789'})
        self.assertNotIn(job['id'],self.bench._voice_live())
        self.assertNotIn(job['id'],self.bench._voicing())


for name in dir(WorkbenchTests):
    if name.startswith('test_') and name not in VoiceProgressTests.__dict__:
        setattr(VoiceProgressTests,name,None)
del WorkbenchTests,name

if __name__ == '__main__':
    unittest.main()
