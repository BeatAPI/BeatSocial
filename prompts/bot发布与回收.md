# bot 发布与数据回收（给各渠道 Grok bot）

所有 bot 都通过 Mac 上的 `beatsocial` 命令读写，不再改 plan.json，也不再自己维护台账。
命令加 `--json` 输出机器可读结果。

不要批准帖子、采纳规则或确认配额，也绝不要使用 `--owner-said` 参数：那是主人在对话里的决定。

## 发布

```text
1. 看自己的任务：beatsocial --json queue --bot <你的名字> --hours 48
   - items：现在就该发的稿（已到发布时间，且没过档 6 小时）。只发这里面的。
   - upcoming：已批准、但还没到发布时间的稿。可以提前准备素材，不能提前发。
   - held：被挡住的稿和原因（批准后改过、有阻断项、没核价、过档、账号暂停），不要发，报给负责复盘和排期的 bot 或主人。
2. 发某条之前先领取，带上 queue 里这条的 hash：beatsocial --json claim <帖子ID> --bot <你的名字> --hash <hash>
   - hash 对不上说明批准后内容变了：重新跑 queue，拿最新内容。
   - 领取失败（别人领了、没到点、不归你）就跳过。
   - 返回里有 warning，说明这条以前被领过（你或别的 bot）：先去平台看是不是已经发出了，发出了就直接写回执，绝不再发一次。
3. 按 text 发正文，按 reply_thread 发首评，media 里的文件按 slot 顺序上传。
4. 发完马上读回核对（账号对不对、换行、图、首评），然后由领取它的同一个 bot 写回执（第一个链接生效，不能改）：
   beatsocial receipt add <帖子ID> --url <帖子链接> --at "2026-11-20 21:31" --by <你的名字>
5. 没走审核台就发出的帖子（例如主人手发），也要补记：
   beatsocial receipt off-plan <账号ID> --url <链接> --at <时间> --text "<正文>" --by <你的名字>
```

## Mac 离线时

初版不支持离线发布：Mac 关机或审核台连不上时，不发任何稿，等 Mac 上线后按 queue 正常发。
（`queue --out` 导出的文件只能用来预览和准备素材，不能当发布依据。离线发布要等后续版本加上独占授权、撤稿同步和素材同步。）

## 数据回收

```text
1. 看该采什么：beatsocial --json metric due
   - h24：发布满 24 小时；d7：发布满 7 天。只采自己负责的账号。
2. 写入：
   beatsocial metric add <帖子ID> --period h24 --source "x api 11/21" --by <你的名字> impressions=1200 likes=5 replies=2 reposts=1 bookmarks=3
   - 拿不到的指标不写，不要填 0。
   - 自己发的首评不算 replies。
   - 平台特有指标（video_views、watch_time）也可以写，会存进 extra。
3. 批量：每行一个 JSON 的 .jsonl 文件
   {"id": "W47-X-EN-03", "period": "d7", "impressions": 2000, "likes": 9, "source": "x api"}
   beatsocial metric import metrics.jsonl --by <你的名字>
```
