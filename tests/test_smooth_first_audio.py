import unittest

from scripts.experimental_smooth_audio import protected_cut


class SmoothTailTests(unittest.TestCase):
    def test_phrasal_verb_not_split_before_particle(self):
        prefix='The shell acts like armor that they cannot easily take '
        self.assertIsNone(protected_cut(prefix,10,flush=True))
        text=prefix+'off. '
        cut=protected_cut(text,10,final=True)
        self.assertEqual(text[:cut].strip(),text.strip())

    def test_possessive_and_adjective_keep_following_noun(self):
        self.assertIsNone(protected_cut('to protect their ',3,flush=True))
        text='to protect their soft bodies from danger. '
        cut=protected_cut(text,3)
        self.assertIn('their soft bodies',text[:cut])

    def test_final_short_tail_merges_without_rewriting(self):
        text='This helps them play outside in the rain.'
        cut=protected_cut(text,6,final=True)
        self.assertEqual(text[:cut],text)


if __name__=='__main__':unittest.main()
