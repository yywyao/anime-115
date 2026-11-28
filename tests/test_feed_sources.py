import base64
import hashlib
import json
import time
import unittest
from datetime import datetime,timezone
from email.utils import format_datetime
from mikan115.errors import AppError
from mikan115.feed_sources import FeedSources
from mikan115.feed_matching import bound_reason
from tests.test_workflow import Harness
from tests.mock_services import HASH


def feed(title='[Test] Test Show S02E01 [1080p][简日]',ident='b'*40,date=None):
    return ('<rss xmlns:nyaa="https://nyaa.si/xmlns/nyaa"><channel><item><title>'+title+'</title><guid>entry</guid>'+
        ('<pubDate>'+format_datetime(datetime.fromtimestamp(date,timezone.utc))+'</pubDate>' if date else '')+
        '<nyaa:infoHash>'+ident+'</nyaa:infoHash></item></channel></rss>').encode()


class FeedAdapters(Harness):
    def test_supported_search_links_are_canonicalized_and_unsafe_sources_rejected(self):
        f=self.app.rss.feeds
        for raw,provider,fragment in [
            ('https://share.dmhy.org/topics/list?keyword=Test&team_id=12','dmhy','/topics/rss/rss.xml?'),
            ('https://nyaa.si/?f=0&c=1_0&q=Test','nyaa','page=rss'),
            ('https://acg.rip/?term=Test','acgrip','/.xml?'),
            (self.mock.base+'/Home/Bangumi/236#7','mikan','subgroupid=7')]:
            source=f.normalize(raw);self.assertEqual(source.provider,provider);self.assertIn(fragment,source.url)
        for raw in ['http://localhost/a','https://nyaa.si:443@evil.test/','https://nyaa.si:81/','https://nyaa.si/download/1.torrent','https://acg.rip/.xml?url=http://localhost','https://share.dmhy.org/topics/rss/rss.xml?keyword=a&keyword=b','https://nyaa.si/%2e%2e/']:
            with self.subTest(raw=raw),self.assertRaises(AppError):f.normalize(raw)

    def test_base32_magnet_namespaced_hash_and_atom_are_parsed(self):
        f=self.app.rss.feeds;source=f.normalize('https://share.dmhy.org/topics/list?keyword=Test')
        infohash='ab'*20;b32=base64.b32encode(bytes.fromhex(infohash)).decode()
        raw=('<rss><channel><item><title>[Team] Test Show - 01 [简繁日][1080p]</title><enclosure url="magnet:?xt=urn:btih:'+b32+'"/></item></channel></rss>').encode()
        item=f.entries(source,raw)[0];self.assertEqual(item['infohash'],infohash);self.assertEqual(item['release']['version'],'chs-cht-ja')
        self.assertTrue(item['release']['group_id'].startswith('dmhy:'))
        nyaa=f.entries(f.normalize('https://nyaa.si/'),feed())[0];self.assertEqual(nyaa['release']['season'],2);self.assertEqual(nyaa['release']['episodes'],[1])
        atom=b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Test Show - 03</title><link rel="enclosure" href="https://acg.rip/t/123.torrent"/></entry></feed>'
        self.assertEqual(f.entries(f.normalize('https://acg.rip/'),atom)[0]['release']['episodes'],[3])

    def test_torrent_fetch_does_not_proxy_arbitrary_hosts_or_routes(self):
        f=self.app.rss.feeds;source=f.normalize('https://nyaa.si/')
        for url in ['http://127.0.0.1/a.torrent','https://nyaa.si/user/123','https://nyaa.si/download/1.torrent?redirect=1','https://nyaa.si:bad/download/1.torrent','https://nyaa.si@evil.test/download/1.torrent']:
            self.assertEqual(f.torrent_url(source,url),'')
        self.assertEqual(f.torrent_url(source,'https://nyaa.si/download/123.torrent'),'https://nyaa.si/download/123.torrent')

    def test_work_page_fragment_must_be_an_actual_rss_subgroup(self):
        with self.assertRaises(AppError):self.app.rss.prepare({'rss_url':self.mock.base+'/Home/Bangumi/236#212'})
        self.assertEqual(self.app.rss.prepare({'rss_url':self.mock.base+'/Home/Bangumi/236#7'})['subgroup_id'],'7')


class SharedFollowing(Harness):
    def setUp(self):
        super().setUp();self.bind();self.payload=b'<rss><channel/></rss>'
        self.catalog.save_works([{'id':'mikan:236','mikan_id':'236','title':'测试动漫','series_title':'测试动漫','aliases':['Test Show'],
            'tmdb_id':77,'tmdb_season':2,'tmdb_mapping':{'status':'linked','id':77,'type':'tv','season':2},
            'known_seasons':[1,2],'episodes':[{'season_number':2,'episode_number':1,'air_date':'2020-01-01'}]}])
        class FakeHTTP:
            def bytes(inner,method,url,**kwargs):
                if not url.startswith(('https://nyaa.si/','https://share.dmhy.org/','https://acg.rip/')):raise AssertionError(url)
                return self.payload
        self.app.rss.feeds.http=FakeHTTP()

    def follow(self,provider='nyaa',**kwargs):
        result=self.app.rss.create(self.user,{'provider':provider,'work_id':'mikan:236',**kwargs})
        return self.store.one('SELECT * FROM subscriptions WHERE id=?',(result['id'],))

    def test_external_rss_requires_explicit_tmdb_work_and_uses_canonical_title(self):
        with self.assertRaises(AppError):self.app.rss.prepare({'rss_url':'https://nyaa.si/?q=Test'})
        sub=self.follow(title='wrong custom title');self.assertEqual(sub['title'],'测试动漫')
        self.assertEqual(sub['scope_key'],'tmdb:tv:77:s2');self.assertNotIn('https://',json.dumps(self.app.rss.list(self.user)))

    def test_saved_external_group_uses_its_name_instead_of_internal_id(self):
        self.payload=feed();source=self.app.rss.feeds.normalize('https://nyaa.si/')
        group=self.app.rss.feeds.entries(source,self.payload)[0]['release']['group_id']
        self.follow(rules={'group_ids':[group]})
        public=self.app.rss.list(self.user)[0]
        self.assertEqual(public['groups'],[{'id':group,'name':'Test'}])

    def test_home_and_other_rss_sources_share_durable_claims(self):
        sub=self.follow();self.payload=feed();self.app.rss.poll(sub)
        self.assertEqual(self.state.submit_count,1)
        # Another provider's mirror with another BTIH is the same confirmed episode.
        other=self.app.rss.create(self.user,{'provider':'dmhy','work_id':'mikan:236'})
        self.assertTrue(other['source_added']);self.assertEqual(other['id'],sub['id'])
        self.payload=feed(ident='c'*40);self.app.rss.poll(sub);self.assertEqual(self.state.submit_count,1)
        self.assertEqual(len(self.store.all('SELECT * FROM subscription_sources')),2)
        from mikan115.following import RSS
        restarted=RSS(self.store,self.config,self.catalog,self.app.workflow);restarted.feeds.http=self.app.rss.feeds.http
        restarted.poll(sub);self.assertEqual(self.state.submit_count,1)

    def test_wrong_work_ambiguous_sequel_and_unknown_episode_do_not_submit(self):
        sub=self.follow()
        for title in ['[Test] Other Show S02E01 [简日]','[Test] Test Show - 01 [简日]','[Test] Test Show S03E01 [简日]','[Test] Test Show [1080p]']:
            self.payload=feed(title);self.app.rss.poll(sub)
        self.assertEqual(self.state.submit_count,0)

    def test_creation_cutoff_and_explicit_history_opt_in(self):
        sub=self.follow();self.payload=feed(date=time.time()-86400);self.app.rss.poll(sub);self.assertEqual(self.state.submit_count,0)
        self.app.rss.update(self.user,sub['id'],{'remove':True})
        # A separate source scope can explicitly collect historical releases.
        other=self.follow(provider='dmhy',include_existing=True)
        self.app.rss.poll(other);self.assertEqual(self.state.submit_count,1)

    def test_home_queue_race_does_not_duplicate_external_rss_episode(self):
        sub=self.follow();self.payload=feed()
        self.app.batch.selected(self.user,'236',[HASH],{'rename':True})
        self.app.workflow.tick();self.app.rss.poll(sub)
        self.assertEqual(self.state.submit_count,1)
        self.assertEqual(len(self.store.all('SELECT * FROM acquisition_claims')),1)

    def test_feed_matching_reuses_one_work_season_lookup_for_a_batch(self):
        work=self.catalog.work('mikan:236');context={'work':work,'season':2};queries=[];original=self.catalog.seed_rows
        def counted(where='',*args,**kwargs):
            if 'json_extract' in where:queries.append(where)
            return original(where,*args,**kwargs)
        self.catalog.seed_rows=counted
        source=self.app.rss.feeds.normalize('https://nyaa.si/')
        for number in range(1,21):
            item=self.app.rss.feeds.entries(source,feed(f'[Test] Test Show S02E{number:02d} [简日]'))[0]
            self.assertEqual(bound_reason(item,context,self.catalog),'')
        self.assertEqual(len(queries),1)
