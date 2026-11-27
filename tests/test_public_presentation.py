import pathlib
import tempfile
import unittest

from mikan115.errors import ProviderError
from mikan115.public_presentation import PublicOverview
from mikan115.store import Store


class FakeHTTP:
    def __init__(self, response=None, error=None):
        self.response=response;self.error=error;self.calls=[]

    def json(self, method, url, **kwargs):
        self.calls.append((method,url,kwargs))
        if self.error:raise self.error
        return self.response


class PublicOverviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(pathlib.Path(self.temp.name))

    def tearDown(self):self.temp.cleanup()

    def test_exact_bangumi_id_fetches_and_caches_chinese_summary(self):
        http=FakeHTTP({'id':624085,'summary':'这是一个公开的中文简介。','images':{'large':'https://lain.bgm.tv/pic/cover/l/c6/8d/624085_5dXDL.jpg'}})
        client=PublicOverview(self.store,http,ttl=600)
        self.assertEqual(client.refresh('624085'),'这是一个公开的中文简介。')
        self.assertEqual(client.cached('624085'),'这是一个公开的中文简介。')
        self.assertEqual(http.calls[0][1],'https://api.bgm.tv/v0/subjects/624085')
        self.assertEqual(client.refresh('624085'),'这是一个公开的中文简介。')
        self.assertEqual(len(http.calls),1)

    def test_keeps_chinese_when_bangumi_includes_the_japanese_original(self):
        chinese='少女在未來城鎮和朋友一起冒險。'
        japanese='少女はその町で出会った友達といっしょにいろいろな場所を旅していきます。新しい仲間との物語です。'
        http=FakeHTTP({'id':624085,'summary':chinese+'\r\n\r\n'+japanese})
        client=PublicOverview(self.store,http)
        self.assertEqual(client.refresh('624085'),'少女在未来城镇和朋友一起冒险。')

    def test_same_response_caches_exact_cover_without_duplicate_fetch(self):
        http=FakeHTTP({'id':624085,'summary':'这是一个公开的中文简介。','images':{'large':'https://lain.bgm.tv/pic/cover/l/c6/8d/624085_5dXDL.jpg'}})
        client=PublicOverview(self.store,http,ttl=600)
        result=client.refresh('624085',need_poster=True)
        self.assertEqual(result['poster_url'],'https://lain.bgm.tv/pic/cover/l/c6/8d/624085_5dXDL.jpg')
        self.assertEqual(client.cached_poster('624085'),'https://lain.bgm.tv/pic/cover/l/c6/8d/624085_5dXDL.jpg')
        self.assertFalse(client.should_refresh('624085',need_overview=True,need_poster=True))
        self.assertEqual(len(http.calls),1)

    def test_foreign_summary_does_not_replace_stale_chinese_text(self):
        self.store.cache_set('bangumi:overview-zh:624085','旧的中文简介。',-1)
        http=FakeHTTP({'id':624085,'summary':'これは日本語の概要です。','images':{}})
        client=PublicOverview(self.store,http,retry_ttl=600)
        self.assertEqual(client.refresh('624085'),'旧的中文简介。')
        self.assertEqual(client.cached('624085',stale=True),'旧的中文简介。')
        self.assertEqual(client.refresh('624085'),'旧的中文简介。')
        self.assertEqual(len(http.calls),1)

    def test_upstream_failure_keeps_stale_text_and_uses_retry_window(self):
        self.store.cache_set('bangumi:overview-zh:624085','旧的中文简介。',-1)
        http=FakeHTTP(error=ProviderError('UPSTREAM_UNAVAILABLE','暂不可用'))
        client=PublicOverview(self.store,http,retry_ttl=600)
        self.assertEqual(client.refresh('624085'),'旧的中文简介。')
        self.assertFalse(client.should_refresh('624085'))
        self.assertEqual(client.refresh('624085'),'旧的中文简介。')
        self.assertEqual(len(http.calls),1)

    def test_blank_legacy_cache_is_refreshed_and_invalid_cover_is_rejected(self):
        self.store.cache_set('bangumi:overview-zh:624085','',600)
        self.store.cache_set('bangumi:poster:624085','https://lain.bgm.tv/pic/cover/0.jpg',600)
        http=FakeHTTP({'id':624085,'summary':'新的中文简介。','images':{'large':'https://lain.bgm.tv/pic/cover/0.jpg','medium':'https://lain.bgm.tv/r/800/pic/cover/l/c6/8d/624085_5dXDL.jpg'}})
        client=PublicOverview(self.store,http,ttl=600)
        self.assertTrue(client.should_refresh('624085',need_overview=True,need_poster=True))
        result=client.refresh('624085',need_poster=True)
        self.assertEqual(result['overview'],'新的中文简介。')
        self.assertEqual(result['poster_url'],'https://lain.bgm.tv/r/800/pic/cover/l/c6/8d/624085_5dXDL.jpg')

    def test_cover_allowlist_rejects_credentials_queries_fragments_and_zero(self):
        good='https://lain.bangumi.tv/pic/cover/l/c6/8d/624085_5dXDL.jpg'
        self.assertEqual(PublicOverview.poster_url(good),good)
        for url in [
            'https://user:pass@lain.bgm.tv/pic/cover/a.jpg',
            'https://lain.bgm.tv/pic/cover/a.jpg?x=1',
            'https://lain.bgm.tv/pic/cover/a.jpg#fragment',
            'http://lain.bgm.tv/pic/cover/a.jpg',
            'https://lain.bgm.tv/pic/cover/0.jpg',
            'https://example.com/pic/cover/a.jpg',
            'https://lain.bgm.tv/r/0/pic/cover/a.jpg',
            'https://lain.bgm.tv/images/a.jpg',
        ]:self.assertEqual(PublicOverview.poster_url(url),'')

    def test_wrong_id_response_is_not_cached(self):
        http=FakeHTTP({'id':624086,'summary':'错误作品'})
        client=PublicOverview(self.store,http,retry_ttl=600)
        self.assertEqual(client.refresh('624085'),'')
        self.assertEqual(client.cached('624085',stale=True),'')

    def test_invalid_ids_never_call_public_source(self):
        http=FakeHTTP({'id':1,'summary':'不应请求'})
        client=PublicOverview(self.store,http)
        self.assertEqual(client.refresh('624085x'),'')
        self.assertEqual(client.cached('624085x'),'')
        self.assertEqual(http.calls,[])


if __name__=='__main__':unittest.main()
