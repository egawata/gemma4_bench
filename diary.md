## Redis で INCR + EXPIRE

Redis を使って rate limit を実現したい時など、例えば1分間に受けるリクエスト数を100に制限したい時、以下のような実装が考えられる。

1リクエストを受けるごとに key REQ_COUNT を INCR して 1 増やす
初回の INCR が行われたときのみ EXPIREで TTL を 60 秒と設定する。
初回 INCR から 60 秒経過すると、key REQ_COUNT は無効となる＝リセットされる
INCR 後の結果が 100 を超えていたらリクエストを拒否
Redis による rate limit 実装の図解

(EXPIRE 実行を初回 INCR 後のみに制限する理由は、2回目以降も実行するとそこからさらに 60 秒後に TTL が伸びてしまうから)

これをもとに、例えば以下のようなコードを書くとする。

```go
// import "github.com/redis/go-redis/v9"
// rdb := redis.NewClient(...)

val, err := rdb.Incr(ctx, "REQ_COUNT").Result()
if err != nil {
    // error handling ...
}
if val == 1 {  // 1 なら初回とみなす
    if err := rdb.Expire(ctx, "REQ_COUNT", 60*time.Second).Err(); err != nil {
        // error handling ...
    }
}
if val > 100 {
    // リクエストを拒否
}
```

これで問題なさそうに見える。
しかしINCR に成功して EXPIRE に失敗するケースで問題が起こる。

EXPIRE に失敗すると、この key は TTL 未設定の状態となる。
しかも2回目以降に同じ処理が行われるときには、 INCR 後の REQ_COUNT は 2 以上になっているはずなので、EXPIRE は決して実行されない。
結果、REQ_COUNT は永久にリセットされないまま、rate limit を迎えたあとも無限にインクリメントされていき、リクエストは拒否され続ける。
永遠に。苔のむすまで。

じゃ EXPIRE に失敗したらエラーハンドリング中にデクリメントしたらよいかというと、並行実行の環境ではそれでも上手くいかない。
(そもそもカウンタの値を減らしていいのかという仕様上の問題を無視するとしても)
例えば2つのプロセスが並列でほぼ同時に上記を実行開始し、2つとも INCR に成功した後に EXPIRE (これは片方でしか実行されない) に失敗した場合、DECL しても REQ_COUNT の中身は 1 になる。やはり EXPIRE は永久に実行されない。

なので、結果を望ましい順番に並べると

- INCR, EXPIRE 両方とも成功する
- INCR, EXPIRE 両方とも失敗する
  - 一応、再度 EXPIRE を実行して成功する希望が残される
- (これは絶対ダメ) INCR のみ成功し、EXPIRE が失敗する

雑にまとめると「INCR と EXPIRE をアトミックに実行できたら良いよね」ということになる。

INCR  と EXPIRE を個別に Redis サーバにリクエストするから、ネットワーク瞬断などの理由で後者のみ失敗する可能性が高くなる。
であれば、極力まとめて実行し、アトミックに近い状態にすれば良い。
となるとまず思いつくのが MULTI や pipeline だが、この目的では使えない。
INCR の結果を見て EXPIRE を実行するか決定する必要があり、MULTIや pipeline ではこれを実現できない。

となると lua スクリプトで書くのも手段の1つ。

まず INCR を実行→結果を見て EXPIRE を実行する lua スクリプトを定義。

```lua
var incrWithExpireScript = redis.NewScript(`
    local val = redis.call('INCR', KEYS[1])
    if val == 1 then
        redis.call('EXPIRE', KEYS[1], ARGV[1])
    end
    return val
`)
```

そして REQ_COUNT をインクリメントかつ有効期限設定するときは上記を実行する。

```lua
		val, err := incrWithExpireScript.Run(ctx, rdb, []string{"REQ_COUNT"}, 60).Int64()
		if err != nil {
			// error handling...
		}
```

もちろん lua スクリプト内でも INCR が成功し EXPIRE が失敗する可能性はゼロではないが、個別に実行するよりははるかに確実。だと思う。

と書いてはみたものの、1分間の rate limit って目的だったら素直に key 分けたほうがいいかも😇
分単位の suffix をつけて REQ_COUNT:202602031234 みたいな key にすれば、事実上有効期限は1分間に制限される。
あとでバッチ等で削除する必要があり、その点は手間かもしれないが。

ちなみに Redis プロジェクトの github issue で、INCR と、初回限定の EXPIRE を同時に実行するコマンドが欲しいという機能リクエストが上がってる。
やっぱり需要はあるのね😅

[NEW] Add a new command increx · Issue #14278 · redis/redis

In our daily work, we frequently encounter a common business need: combining INCR with EXPIRE.
INCREX ってコマンドを追加したらいいんじゃないか、という元の提案があり、それに対して INCR コマンドに EX (second) をつけるとかして INCR REQ_COUNT EX 60 みたいに実行できればいいんじゃないの、などの別案も出ている。

まぁ lua スクリプトでゴリゴリ書くのも力技感を否めないし、1つのコマンドでスッキリ実現できたら便利ではある。
