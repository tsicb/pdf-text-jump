PDF Text Jump Viewer v2（複数PDF横断検索版）

■ 配置先
リポジトリ：pdf-text-jump

pdf-text-jump/
├─ index.html
├─ pdfs.json
└─ indeedmarketreports/
   ├─ report-a.pdf
   ├─ report-b.pdf
   └─ report-c.pdf

■ PDF一覧の取得方法
1. 最初に pdfs.json を読み込みます。
2. pdfs.json の files が空、または未設置の場合は、
   GitHub APIから indeedmarketreports フォルダ内のPDFを自動取得します。

同梱の pdfs.json は files が空なので、配置直後はGitHub API方式で動きます。
そのため、既存PDFのファイル名をpdfs.jsonへ手入力しなくても選択肢が表示されます。

■ 表示名や並び順を管理したくなった場合
pdfs.json を次のように編集します。

{
  "baseDir": "indeedmarketreports/",
  "files": [
    {
      "file": "report-202607.pdf",
      "label": "2026年7月 Indeed市場レポート",
      "selected": true
    },
    {
      "file": "report-202606.pdf",
      "label": "2026年6月 Indeed市場レポート",
      "selected": true
    }
  ]
}

・file：indeedmarketreports内の実ファイル名
・label：画面に表示する名称
・selected：初期選択するか（省略時はtrue）
・enabled:false を付けると一覧から除外できます

■ URL共有
複数のfileパラメータを指定できます。

https://tsicb.github.io/pdf-text-jump/?file=report-a.pdf&file=report-b.pdf&text=有効求人倍率

■ 今回の実装範囲
・複数PDFの選択UI
・pdfs.jsonまたはGitHub APIによるPDF一覧取得
・選択したPDFの横断検索
・同一ページの重複排除
・PDF別のヒットページ一覧
・結果クリックで元PDFの該当ページを表示
・検索語のページ内ハイライト
・既存の前後ページ移動、ページ番号指定、ズーム

■ 次段階として予定できる機能
・ヒットページだけの縦スクロール連続表示
・複数PDFのヒットページを1つのPDFへ結合
・結合PDFのダウンロード

■ 注意
・GitHub API方式は公開リポジトリを前提とします。
・GitHub APIには未認証アクセスの回数制限があります。
  通常の少人数利用では問題になりにくいですが、安定運用ではpdfs.json方式が推奨です。
・画像だけのスキャンPDFは文字検索できません。
・PDF内部の文字分割によって、ページは見つかってもハイライトできないことがあります。
