"""rename_engine 的数据驱动回归测试。

用标准库unittest,不引入pytest——requirements.txt会被打进PyInstaller包,
测试依赖不该跟着进发行版。

运行: cd server && python -m unittest discover -s tests -v

语料 fixtures/rename_golden.json 的两类条目:
  known_bug=false 的是**当前行为快照**(取自Auto_Bangumi的golden corpus和
    issue复现语料),不代表"这就是对的",只用来兜住"改解析逻辑时别把没打算动的
    条目一起改了"。
  known_bug=true 的是**手工判定的正确期望**,覆盖已确诊的解析缺陷和
    "正片不能被误判成剧场版/OVA"这类negative case。
"""
import json
import unittest
from pathlib import Path

import rename_engine as R

FIXTURE = Path(__file__).parent / "fixtures" / "rename_golden.json"

# 语料只考解析行为,所以番名/季号这些"由调用方从Bangumi解析得来"的入参固定成常量,
# 把它们的影响从断言里排除掉(见preview_rename_file的docstring:anime_title和
# season_hint本来就不从种子标题里取)。
FIXED_TITLE = "番"
FIXED_ROOT = "D:\\Lib"

# preview_rename_file 返回值的完整字段集。加了新字段却忘了更新语料/测试时,
# _test_result_fields 会立刻红——照抄Auto_Bangumi的golden测试做法。
EXPECTED_RESULT_FIELDS = {
    "original_file_name",
    "media_type",
    "work_title_from_hint",
    "parsed_episode",
    "release_version",
    "anime_root",
    "relative_path",
    "target_folder",
    "target_filename",
    "target_full_path",
    "target_relative_path",
}


def _load():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def _run(raw, platform):
    """按语料条目跑一遍完整的单文件改名链路。"""
    return R.preview_rename_file(
        FIXED_TITLE, raw, raw, FIXED_ROOT,
        bgm_id=1, season_hint=FIXED_TITLE, season_ordinal="01", platform=platform,
    )


class TestCorpusIntegrity(unittest.TestCase):
    """语料本身的完整性,防止误删/误改。"""

    def test_corpus_shape(self):
        data = _load()
        cases = data["cases"]
        self.assertEqual(data["count"], len(cases), "count 字段跟实际条数对不上")
        self.assertGreaterEqual(len(cases), 100, "语料被删剩不到100条了?")

        ids = [c["id"] for c in cases]
        self.assertEqual(len(ids), len(set(ids)), "语料里有重复的 id")

        raws = [c["raw"] for c in cases]
        self.assertEqual(len(raws), len(set(raws)), "语料里有重复的 raw 标题")

        for c in cases:
            self.assertTrue(c["raw"].strip(), f"{c['id']} 的 raw 是空的")
            self.assertIn(c["media_type"], ("tv", "movie", "ova", "extra"), c["id"])

    def test_result_fields(self):
        """返回值字段集必须跟 EXPECTED_RESULT_FIELDS 完全一致。"""
        result = _run("[Group] Anime Title - 01 [1080p].mkv", "TV")
        self.assertEqual(
            set(result.keys()), EXPECTED_RESULT_FIELDS,
            "preview_rename_file 的返回字段变了,记得同步更新测试与语料",
        )


class TestGoldenCorpus(unittest.TestCase):
    """逐条跑语料。subTest 保证一条失败不挡住其余条目的结果。"""

    def test_media_type(self):
        for c in _load()["cases"]:
            with self.subTest(id=c["id"], raw=c["raw"][:70]):
                # 走完整链路而不是裸 classify_media_type:半集(12.5)要靠解析出的
                # 集数值才能判成 OVA,单独调分类函数传不进这个信息。
                got = _run(c["raw"], c["platform"])["media_type"]
                self.assertEqual(
                    got, c["media_type"],
                    f"[{c['id']}] {'手工判定' if c['known_bug'] else '快照'}: {c['raw']}",
                )

    def test_episode(self):
        for c in _load()["cases"]:
            if c["episode"] is None:
                continue  # 区间/合集类条目不断言集数,语义本身就不是单集
            with self.subTest(id=c["id"], raw=c["raw"][:70]):
                got = _run(c["raw"], c["platform"])["parsed_episode"]
                self.assertEqual(
                    got, c["episode"],
                    f"[{c['id']}] {'手工判定' if c['known_bug'] else '快照'}: {c['raw']}",
                )


class TestChineseSeasonNumber(unittest.TestCase):
    """Bug 3: 中文季度数字此前只支持个位,"第十二季"会退回 Season 01。"""

    CASES = [
        ("[Group] 某作品 第一季 - 08 [1080p].mkv", "01"),
        ("[Group] 某作品 第二季 - 08 [1080p].mkv", "02"),
        ("[Group] 某作品 第十季 - 08 [1080p].mkv", "10"),
        ("[Group] 某作品 第十二季 - 08 [1080p].mkv", "12"),
        ("[Group] 某作品 第二十四季 - 08 [1080p].mkv", "24"),
        ("[Group] 某作品 第两季 - 08 [1080p].mkv", "02"),
    ]

    def test_chinese_season(self):
        for raw, expected in self.CASES:
            with self.subTest(raw=raw):
                # season_ordinal=None 才会走文本正则猜测这条路径
                result = R.preview_rename_file(
                    FIXED_TITLE, raw, raw, FIXED_ROOT,
                    bgm_id=None, season_hint=FIXED_TITLE, season_ordinal=None,
                    platform="TV",
                )
                self.assertIn(
                    f"S{expected}E", result["target_filename"],
                    f"季号解析错: {raw} -> {result['target_filename']}",
                )


class TestRangeTorrentFallback(unittest.TestCase):
    """Bug 4: 种子标题是区间包(如 [01-12])时,不许拿区间起点当单集回退值。

    否则种子内多个"文件名自身没有集数"的文件会全部拿到 01、撞成同一个目标路径。
    撞车本身由 organize.py 的 _guard_target_path_collisions 兜住(不会丢文件),
    但撞上的文件会被标记 failed 卡在暂存区不动,整理不进媒体库。"""

    TORRENT = "[Group] Anime Title [01-12] [BDRip 1080p]"

    def _run_file(self, file_name):
        return R.preview_rename_file(
            FIXED_TITLE, file_name, self.TORRENT, FIXED_ROOT,
            bgm_id=1, season_hint=FIXED_TITLE, season_ordinal="01", platform="TV",
        )

    def test_no_collision_between_episodeless_files(self):
        names = ["OVA.mkv", "特典映像.mkv", "Anime Title.mkv"]
        produced = {}
        for n in names:
            produced[n] = self._run_file(n)["target_filename"]
        self.assertEqual(
            len(set(produced.values())), len(names),
            f"区间包内无集数的文件撞名了: {produced}",
        )

    def test_range_start_not_used_as_episode(self):
        """文件名自身没有集数时,不该凭空得到区间起点 01。"""
        got = self._run_file("OVA.mkv")["parsed_episode"]
        self.assertNotEqual(got, "01", "把区间起点 01 当成了这个文件的集数")

    def test_disambiguation_is_idempotent(self):
        """library_repair 会对**已经落地**的文件重算目标路径,所以命名必须幂等,
        否则每跑一次修复文件名就长一截([Group][1080p] 会被反复追加)。"""
        name = "OVA.mkv"
        first = self._run_file(name)["target_filename"]
        second = self._run_file(first)["target_filename"]
        third = self._run_file(second)["target_filename"]
        self.assertEqual(first, second, "第二次改名结果就变了")
        self.assertEqual(second, third, "命名不收敛")

    def test_real_episode_in_filename_still_wins(self):
        """常见场景不能被上面的守卫误伤:文件名自带集数时照常解析。"""
        got = self._run_file("[Group] Anime Title - 07 [BDRip 1080p].mkv")
        self.assertEqual(got["parsed_episode"], "07")


class TestLibraryRepairRecompute(unittest.TestCase):
    """模拟「修复媒体库」的重算路径,锁住几条不变量。

    library_repair 重算目标路径时,喂给 preview_rename_file 的是
    RenamedFile.original_path(种子内原始文件名)+ RenamedFile.torrent_title
    (当初落地时的种子标题),而不是磁盘上已经改好的名字。
    这里直接复用 library_repair 真实的参数映射函数,避免测试跟实现漂移。
    """

    # 合集包:元数据(字幕组/分辨率)只存在于种子标题里,包内文件名很裸。
    # 这正是 organize 与 repair 最容易算出不同结果的场景。
    PACK_TORRENT = "[VCB-Studio] Anime Title [01-12] [BDRip 1080p HEVC]"
    PACK_FILES = ["01.mkv", "05.mkv", "OVA.mkv", "特典映像.mkv"]
    # 单集种子:文件名自带完整元数据,两条路径本来就该一致。
    SINGLE_TORRENT = "[LoliHouse] 葬送的芙莉莲 / Sousou no Frieren - 03 [WebRip 1080p HEVC-10bit AAC]"
    SINGLE_FILE = "[LoliHouse] 葬送的芙莉莲 - 03 [WebRip 1080p HEVC-10bit AAC].mkv"

    def _organize(self, file_name, torrent_title, **kw):
        """下载完成后的整理路径:torrent_title 是 qBittorrent 里真实的种子名。"""
        args = {"season_ordinal": "01", "platform": "TV", "episode_offset": 0,
                "season_total_eps": None}
        args.update(kw)
        return R.preview_rename_file(
            FIXED_TITLE, file_name, torrent_title, FIXED_ROOT,
            bgm_id=1, season_hint=FIXED_TITLE, **args,
        )

    def _repair(self, file_name, stored_torrent_title, **kw):
        """修复路径:file_name 取 RenamedFile.original_path,
        torrent_title 取 RenamedFile.torrent_title(本次新增的列)。"""
        return self._organize(file_name, stored_torrent_title, **kw)

    def test_organize_and_repair_agree(self):
        """最关键的一条:同一个文件,整理路径和修复路径必须算出完全相同的目标路径。

        不一致就意味着「修复媒体库」会对这些文件永远提议改名、永不收敛——
        这正是 RenamedFile.torrent_title 这一列存在的理由。
        """
        for torrent, files in (
            (self.PACK_TORRENT, self.PACK_FILES),
            (self.SINGLE_TORRENT, [self.SINGLE_FILE]),
        ):
            for fn in files:
                with self.subTest(torrent=torrent[:40], file=fn):
                    landed = self._organize(fn, torrent)["target_full_path"]
                    # 落地时把种子标题存进了 RenamedFile,修复时原样喂回来
                    recomputed = self._repair(fn, torrent)["target_full_path"]
                    self.assertEqual(
                        landed, recomputed,
                        "整理与修复算出的目标路径不一致,修复会无限提议改名",
                    )

    def test_pack_without_stored_title_would_diverge(self):
        """反证:如果不存种子标题、退回拿文件名当种子标题,合集包裸文件名会丢掉
        "[字幕组][分辨率]"后缀,跟落地结果对不上。这条记录的就是
        RenamedFile.torrent_title 这一列存在的理由,删了它就会退回这个坏行为。"""
        landed = self._organize("05.mkv", self.PACK_TORRENT)["target_filename"]
        naive = self._organize("05.mkv", "05.mkv")["target_filename"]
        self.assertNotEqual(
            landed, naive,
            "预期裸文件名会丢元数据后缀;若两者相同说明元数据来源变了,本测试需重写",
        )
        self.assertIn("VCB-Studio", landed)
        self.assertNotIn("VCB-Studio", naive)

    def test_legacy_row_without_torrent_title_still_recomputes(self):
        """升级前落地的老行没有 torrent_title,修复时退回用原始文件名当种子标题。
        这条只要求不炸、且结果稳定,不要求跟落地结果一致(老数据本来就没这个信息)。"""
        for fn in self.PACK_FILES:
            with self.subTest(file=fn):
                once = self._repair(fn, fn)["target_full_path"]
                twice = self._repair(fn, fn)["target_full_path"]
                self.assertEqual(once, twice)
                self.assertTrue(once, "重算结果不该为空")

    def test_repair_converges(self):
        """反复重算必须收敛:修复功能可能被跑很多次,每次都提议改名是不可接受的。"""
        for fn in self.PACK_FILES:
            with self.subTest(file=fn):
                first = self._repair(fn, self.PACK_TORRENT)["target_full_path"]
                second = self._repair(fn, self.PACK_TORRENT)["target_full_path"]
                self.assertEqual(first, second)

    def test_no_sibling_collision(self):
        """同一个合集包内的兄弟文件,算出的目标路径必须互不相同,
        否则撞车的那些会被 organize 的安全网标记 failed、卡在暂存区进不了媒体库。"""
        targets = [
            self._organize(fn, self.PACK_TORRENT)["target_full_path"]
            for fn in self.PACK_FILES
        ]
        self.assertEqual(
            len(set(targets)), len(targets), f"合集包内文件撞名: {targets}"
        )

    def test_bucket_args_match_landing_bucket(self):
        """按桶重算出来的落地桶,必须还是这个桶本身——即"对一个已经正确归位的库
        跑修复,应该一条建议都提不出来"。参数映射直接用 library_repair 的真函数。"""
        from services import library_repair as LR

        cases = [
            ("OVA", "OVA.mkv", "OVA"),
            ("剧场版", "某剧场作品 Movie.mkv", "剧场版"),
            ("Other", "[Group] Anime NCOP [1080p].mkv", "Other"),
        ]
        for bucket, file_name, expected_bucket in cases:
            with self.subTest(bucket=bucket, file=file_name):
                args = LR._bucket_recompute_args(bucket, extra_buckets=set())
                self.assertIsNotNone(args, f"{bucket} 桶拿不到重算参数")
                result = R.preview_rename_file(
                    FIXED_TITLE, file_name, file_name, FIXED_ROOT,
                    bgm_id=1, season_hint=FIXED_TITLE, **args,
                )
                landed_bucket = result["target_folder"].rstrip("\\").rsplit("\\", 1)[-1]
                self.assertEqual(
                    landed_bucket, expected_bucket,
                    f"{bucket} 桶里的文件被重算到了 {landed_bucket},修复会把它搬走",
                )

    def test_half_episode_stays_in_season(self):
        """半集(12.5)跟正片同季,命名 S01E12.5——小数不能取整,否则会跟同季的
        第12集撞成同一个文件名互相覆盖。"""
        result = self._organize(
            "[EMBER] Frieren - 12.5 [BDRip 1080p].mkv", self.SINGLE_TORRENT
        )
        self.assertEqual(result["media_type"], "tv")
        self.assertTrue(
            result["target_folder"].endswith("Season 01"),
            f"半集没留在正片季目录: {result['target_folder']}",
        )
        self.assertIn("S01E12.5", result["target_filename"])

    def test_half_episode_zero_padded(self):
        """个位半集补零成 09.5,跟整数集的两位补零观感一致。"""
        result = self._organize("[Group] Anime - 9.5 [1080p].mkv", self.SINGLE_TORRENT)
        self.assertIn("S01E09.5", result["target_filename"])

    def test_half_episode_cross_season_offset(self):
        """跨季绝对编号的半集也要能换算(实测:转生史莱姆第三季发的是 48.5)。"""
        result = self._organize(
            "[ANi] 史莱姆 第三季 - 48.5 [1080P][Baha].mp4", self.SINGLE_TORRENT,
            season_ordinal="03", episode_offset=24, season_total_eps=24,
        )
        self.assertIn("S03E24.5", result["target_filename"])


class TestSourceLookupPlumbing(unittest.TestCase):
    """用内存SQLite跑通 RenamedFile.torrent_title 的真实读写链路。

    上面那些测试只验证「拿到标题之后算得对」,这里验证「标题真的存得进去、
    也真的读得回来」,并覆盖老数据(该列为空)的退化路径。
    """

    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import models

        self.engine = create_engine("sqlite:///:memory:")
        models.Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_roundtrip_and_legacy_fallback(self):
        from services import library_repair as LR
        from services.staging import upsert_renamed_file

        db = self.Session()
        try:
            # 新数据:整理时把种子标题一起存下来
            upsert_renamed_file(
                db, "hash_new", "01.mkv", status="done",
                target="番 [bgm-1]/Season 01/番 - S01E01 [VCB-Studio][1080p].mkv",
                torrent_title="[VCB-Studio] Anime Title [01-12] [BDRip 1080p]",
            )
            # 老数据:升级前落地的行没有这一列
            upsert_renamed_file(
                db, "hash_old", "02.mkv", status="done",
                target="番 [bgm-1]/Season 01/番 - S01E02.mkv",
            )
            # failed 的行不该出现在查找表里(修复只重算已成功落地的文件)
            upsert_renamed_file(
                db, "hash_bad", "03.mkv", status="failed", error="x",
                torrent_title="不该被读到",
            )

            lookup = LR._source_file_name_lookup(db)

            new = lookup[LR._same_relpath("番 [bgm-1]/Season 01/番 - S01E01 [VCB-Studio][1080p].mkv")]
            self.assertEqual(new[0], "01.mkv")
            self.assertEqual(new[1], "[VCB-Studio] Anime Title [01-12] [BDRip 1080p]")

            old = lookup[LR._same_relpath("番 [bgm-1]/Season 01/番 - S01E02.mkv")]
            self.assertEqual(old[0], "02.mkv")
            self.assertEqual(old[1], "02.mkv", "老行该退回用原始文件名当种子标题")

            self.assertEqual(len(lookup), 2, "status!=done 的行不该进查找表")
        finally:
            db.close()

    def test_none_does_not_wipe_existing_title(self):
        """失败重试之类的路径不传 torrent_title,不能把已存的标题擦掉。"""
        from services.staging import upsert_renamed_file
        import models

        db = self.Session()
        try:
            upsert_renamed_file(
                db, "h", "01.mkv", status="done", target="a/b.mkv",
                torrent_title="[Group] Pack [01-12]",
            )
            upsert_renamed_file(db, "h", "01.mkv", status="done", target="a/c.mkv")
            row = db.query(models.RenamedFile).filter_by(torrent_hash="h").one()
            self.assertEqual(row.torrent_title, "[Group] Pack [01-12]")
            self.assertEqual(row.target_relative_path, "a/c.mkv")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
