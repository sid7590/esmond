from django.test import TestCase
from esmond.util import atencode, atdecode, decode_alu_port, build_alu_sap_name

class TestAtEncoding(TestCase):
    def test_basics(self):
        from esmond.util import _atencode_safe, _atencode_unsafe

        self.assertEqual(atencode(_atencode_safe), _atencode_safe)

        self.assertEqual(atencode(_atencode_unsafe),
                '@20@24@26@2B@2C@2F@3A@3B@3D@3F@40@7F')
        self.assertEqual(atdecode(atencode(_atencode_unsafe)), _atencode_unsafe)

        s = ''.join([chr(x) for x in xrange(256)])

        self.assertEqual(atencode(s),
                '@00@01@02@03@04@05@06@07@08@09@0A@0B@0C@0D@0E@0F@10@11@12@13@14@15@16@17@18@19@1A@1B@1C@1D@1E@1F@20@21@22@23@24@25@26@27@28@29@2A@2B@2C-.@2F0123456789@3A@3B@3C@3D@3E@3F@40ABCDEFGHIJKLMNOPQRSTUVWX@59Z@5B@5C@5D@5E_@60abcdefghijklmnopqrstuvwxyz@7B@7C@7D@7E@7F@80@81@82@83@84@85@86@87@88@89@8A@8B@8C@8D@8E@8F@90@91@92@93@94@95@96@97@98@99@9A@9B@9C@9D@9E@9F@A0@A1@A2@A3@A4@A5@A6@A7@A8@A9@AA@AB@AC@AD@AE@AF@B0@B1@B2@B3@B4@B5@B6@B7@B8@B9@BA@BB@BC@BD@BE@BF@C0@C1@C2@C3@C4@C5@C6@C7@C8@C9@CA@CB@CC@CD@CE@CF@D0@D1@D2@D3@D4@D5@D6@D7@D8@D9@DA@DB@DC@DD@DE@DF@E0@E1@E2@E3@E4@E5@E6@E7@E8@E9@EA@EB@EC@ED@EE@EF@F0@F1@F2@F3@F4@F5@F6@F7@F8@F9@FA@FB@FC@FD@FE@FF')

        self.assertEqual(atencode(s, minimal=True),
                '''@00@01@02@03@04@05@06@07@08@09@0A@0B@0C@0D@0E@0F@10@11@12@13@14@15@16@17@18@19@1A@1B@1C@1D@1E@1F@20!"#@24%@26'()*@2B@2C-.@2F0123456789@3A@3B<@3D>@3F@40ABCDEFGHIJKLMNOPQRSTUVWXYZ[\]^_`abcdefghijklmnopqrstuvwxyz{|}~@7F@80@81@82@83@84@85@86@87@88@89@8A@8B@8C@8D@8E@8F@90@91@92@93@94@95@96@97@98@99@9A@9B@9C@9D@9E@9F@A0@A1@A2@A3@A4@A5@A6@A7@A8@A9@AA@AB@AC@AD@AE@AF@B0@B1@B2@B3@B4@B5@B6@B7@B8@B9@BA@BB@BC@BD@BE@BF@C0@C1@C2@C3@C4@C5@C6@C7@C8@C9@CA@CB@CC@CD@CE@CF@D0@D1@D2@D3@D4@D5@D6@D7@D8@D9@DA@DB@DC@DD@DE@DF@E0@E1@E2@E3@E4@E5@E6@E7@E8@E9@EA@EB@EC@ED@EE@EF@F0@F1@F2@F3@F4@F5@F6@F7@F8@F9@FA@FB@FC@FD@FE@FF''')


class TestALUPortDecoder(TestCase):
    def test_decode_alu_port(self):
        tests = [
            (35684352, '1/1/1'),
            (69238784, '2/1/1'),
            (102793216, '3/1/1'),
            (136347648, '4/1/1'),
            (169902080, '5/1/1'),
            (337674240, '10/1/1'),
            (337707008, '10/1/2'),
            (337739776, '10/1/3'),
            (337772544, '10/1/4'),
            (337805312, '10/1/5'),
            (337838080, '10/1/6'),
            (337870848, '10/1/7'),
            (337903616, '10/1/8'),
            (337936384, '10/1/9'),
            (337969152, '10/1/10'),
            (338001920, '10/1/11'),
            (338034688, '10/1/12'),
            (574652416, '1/2/1.0'),
            (574652426, '1/2/1.10'),
            (1073741825, 'virtual-1'),
            (1342177281, 'lag-1'),
            (0x1e000000, 'invalid-portid'),
        ]

        for idx, port in tests:
            self.assertEqual(decode_alu_port(idx), port)

class TestBuildALUSAPName(TestCase):
    def test_build_alu_sap_name(self):
        self.assertEqual(build_alu_sap_name("xxx.111.337969152.2200"), 
                "111-10/1/10-2200")
