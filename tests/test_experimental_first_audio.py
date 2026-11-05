import asyncio
import unittest

from scripts.experimental_first_audio import FirstPhraseBuffer, first_phrase_cut


class FirstAudioIntegrityTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_retains_every_character_and_number_group(self):
        text = 'Nora has twenty five pencils, not twenty. Dr. Green paid 3.50 dollars.'
        buffer = FirstPhraseBuffer(max_wait_ms=5)
        output = []

        async def read():
            for char in text:
                await buffer.append(char)
                await asyncio.sleep(0)
            await buffer.finish()

        async def speak():
            while (segment := await buffer.next(lambda: 0)) is not None:
                output.append(segment)

        await asyncio.gather(read(), speak())
        self.assertEqual(''.join(output), text)
        self.assertIn('twenty five pencils', output[0])

    async def test_negation_waits_for_predicate(self):
        self.assertIsNone(first_phrase_cut('I do not '))
        text = 'I do not know the answer. '
        cut = first_phrase_cut(text)
        self.assertEqual(text[:cut].strip(), 'I do not know')

    async def test_price_and_modified_number_remain_one_assertion(self):
        for text in ['It costs three dollars and fifty cents. ',
                     'Mia has twenty five blue beads. ',
                     'The price is 3.50 dollars, not 35 dollars. ']:
            self.assertIsNone(first_phrase_cut(text.split('.')[0]+' '))
            cut = first_phrase_cut(text)
            self.assertEqual(text[:cut].strip(), text.strip())

    async def test_transitive_openings_do_not_become_tiny_fragments(self):
        for text in ['You can use ', 'A raincoat keeps ', 'Would you like ',
                     'That sounds like ', 'You should not touch ', 'A piano can ']:
            self.assertIsNone(first_phrase_cut(text))

    async def test_short_final_and_mixed_language_not_lost(self):
        for text in ['No.', '樱桃的英文是 cherry。', "Don't touch a hot pan."]:
            buffer = FirstPhraseBuffer()
            await buffer.append(text)
            await buffer.finish()
            segments = []
            while (part := await buffer.next(lambda: 0)) is not None:
                segments.append(part)
            self.assertEqual(''.join(segments), text)


if __name__ == '__main__':
    unittest.main()
