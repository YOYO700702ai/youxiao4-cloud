import unittest
from listing_commands import parse_listing_command, ListingInputError


class ListingCommandsTests(unittest.TestCase):
    def test_full_form_keeps_synopsis_and_normalizes_numbers(self):
        command = parse_listing_command('陸總，上架劇本\n劇本：魔女論破\n人數：7\n時長：12\\~14\n售價：2300\n類型：推理\n標籤：台中獨家、頭腦風暴\n簡介：第一段\n第二段\n人物角色：貪婪／嫉妒')
        self.assertEqual(command['action'], 'begin')
        self.assertEqual(command['data']['人數'], ['7人'])
        self.assertEqual(command['data']['價格'], 2300)
        self.assertEqual(command['data']['時長'], '12~14小時')
        self.assertEqual(command['data']['簡介'], '第一段\n第二段')
        self.assertEqual(command['data']['角色'], ['貪婪', '嫉妒'])

    def test_chat_cannot_finish_or_select_images(self):
        for text in ['大家覺得這张封面好看嗎', '小六，上架要怎麼說？', '他說資料傳完，直接上架就好', '聊一下《魔女論破》的角色圖', '下次再上架']:
            self.assertIsNone(parse_listing_command(text), text)

    def test_exact_finish_and_explicit_batches(self):
        self.assertEqual(parse_listing_command('陸總，資料傳完，直接上架！'), {'action': 'finish'})
        self.assertEqual(parse_listing_command('接下來這 7 張是《魔女論破》的角色圖'), {'action': 'label', 'name': '魔女論破', 'purpose': 'portraits', 'count': 7})
        self.assertEqual(parse_listing_command('接下來這張是《魔女論破》的封面')['count'], 1)
        self.assertEqual(parse_listing_command('接下來七張是角色圖')['count'], 7)

    def test_role_count_required_and_title_conflict_blocks(self):
        with self.assertRaises(ListingInputError):
            parse_listing_command('這批是《魔女論破》的角色圖')
        with self.assertRaises(ListingInputError):
            parse_listing_command('上架《A》\n劇本：B')

    def test_visual_spacing_around_title_keeps_explicit_commands(self):
        self.assertEqual(parse_listing_command('接下來這 7 張是 《魔女論破》 的角色圖'),
                         {'action': 'label', 'name': '魔女論破', 'purpose': 'portraits', 'count': 7})
        self.assertEqual(parse_listing_command('接下來這張是 《魔女論破》 的封面')['purpose'], 'cover')
        self.assertEqual(parse_listing_command('陸總，補 《魔女論破》 的角色圖')['kind'], 'portraits')

    def test_manual_correction_and_existing_portraits(self):
        self.assertEqual(parse_listing_command('配對角色 3：傲慢魔女的親眷')['index'], 3)
        self.assertEqual(parse_listing_command('確認圖片 1 為封面')['action'], 'confirm_cover')
        self.assertEqual(parse_listing_command('陸總，補《魔女論破》的角色圖')['kind'], 'portraits')
        self.assertEqual(parse_listing_command('上架資料\n售價：2,300元')['data']['價格'], 2300)

    def test_duplicate_fields_and_bad_values_not_silently_guessed(self):
        for text in ['上架資料\n價格：800\n售價：900', '上架資料\n價格：八百', '上架資料\n人數：不知道', '上架資料\n角色：A／A']:
            with self.assertRaises(ListingInputError):
                parse_listing_command(text)


if __name__ == '__main__':
    unittest.main()
