// Original, public demo content only. Real books remain behind account and chapter
// authorization in the API (§0.3 / §15); this sample never calls private endpoints.
export const demoChapters = [
  {
    title: "The light across the water",
    paragraphs: [
      "At dusk, Lin climbed the hundred steps to the lantern room. For seven years, the tower on the opposite shore had been dark. Tonight, a single gold light waited in its window.",
      "Mei was still at the ferry landing, mending a rope by the last of the sun. When Lin pointed across the water, she stopped working. From her coat she took a small copper key and pressed it into his palm.",
      "“Wait for low tide,” she said. “And whatever you find, leave a light in the window.” Lin wanted to ask whose key it was, but the ferry bell rang, and Mei had already turned away.",
    ],
    source: [
      "暮色降临时，林走上通往灯室的一百级台阶。对岸的高塔已经暗了七年。今夜，窗里却亮着一点金光。",
      "梅还在渡口，借着最后一线日光修补缆绳。林指向水面那头，她便停下了手中的活。她从衣袋里取出一把小铜钥匙，放进他的掌心。",
      "“等退潮再去。”她说，“不管找到什么，都在窗边留一盏灯。”林想问这是谁的钥匙，可渡船的钟声响了，梅已经转过身去。",
    ],
    question: "Why does Lin go to the tower?",
    answer: "A light appears in the tower after seven dark years. Mei gives Lin a key and tells him to wait for low tide. He does not yet know what he will find there.",
  },
  {
    title: "The keeper’s name",
    paragraphs: [
      "Low tide revealed a path of pale stones. Lin crossed with the copper key held tight. Inside the tower, old tide charts covered the walls, and a keeper’s log lay open beneath the lamp.",
      "The last entry was signed Mei. Lin read it twice. The woman who mended ferry ropes had once tended this light. Beside her name was a sketch of the harbor, with a narrow channel he had never seen on any map.",
      "A footstep sounded behind him. “The sea takes its time returning what we lose,” Mei said. She touched the edge of the chart. “That channel will open again tomorrow. We must be ready.”",
    ],
    source: [
      "退潮后，一条浅色石路露了出来。林握紧铜钥匙走过石路。塔里满墙都是旧潮汐图，灯下摊着一本守灯人的日志。",
      "最后一页签着梅的名字。林读了两遍。那个在渡口补缆绳的女人，原来曾经守着这盏灯。名字旁画着港湾，还有一条他从未在地图上见过的狭窄水道。",
      "身后响起脚步声。“大海归还失物，总要花些时间。”梅说。她碰了碰图纸的边缘。“明天，那条水道会重新打开。我们得准备好。”",
    ],
    question: "What have we learned about Mei?",
    answer: "Mei once kept the tower’s light. Her signature appears in the keeper’s log, and she knows that an old harbor channel will open again tomorrow.",
  },
  {
    title: "A harbor remembered",
    paragraphs: [
      "At dawn, Mei turned the lantern toward the hills instead of the sea. Its light caught a line of mirrors hidden among the rocks. One by one, distant windows answered with sparks of gold.",
      "“It was never only a light for ships,” she told Lin. “The villages watch it too.” When the channel opened, fresh water would reach the old gardens for the first time in seven years. The lantern was their signal to open the gates.",
      "Lin set the copper key beside the log and wrote a new date beneath Mei’s name. Across the water, people were walking toward their gardens. He finally understood what it meant to leave a light in the window.",
    ],
    source: [
      "黎明时，梅把灯转向山丘，而不是大海。灯光照到藏在岩石间的一排镜子。远处的窗户一扇接一扇，闪起金色的光。",
      "“它从来不只是给船看的。”她告诉林，“村里的人也在等它。”水道打开后，淡水将七年来第一次流进旧日的田园。这盏灯，就是让他们开闸的信号。",
      "林把铜钥匙放在日志旁，在梅的名字下面写上新的日期。水的那头，人们正走向田园。他终于明白，在窗边留一盏灯意味着什么。",
    ],
    question: "What is the lantern’s purpose?",
    answer: "It signals the villages to open their gates when the channel returns, bringing fresh water to the gardens. The network of mirrors carries its light into the hills.",
  },
];

export type DemoPerson = "Lin" | "Mei";
const facts: { person: DemoPerson; chapter: number; text: string }[] = [
  { person: "Lin", chapter: 1, text: "Visits the lantern room and notices a light in the abandoned tower." },
  { person: "Lin", chapter: 1, text: "Receives a copper key from Mei." },
  { person: "Mei", chapter: 1, text: "Mends ropes at the ferry landing." },
  { person: "Mei", chapter: 1, text: "Gives Lin a copper key and tells him to wait for low tide." },
  { person: "Lin", chapter: 2, text: "Crosses to the tower and finds Mei’s signature in the keeper’s log." },
  { person: "Mei", chapter: 2, text: "Was the tower’s lantern keeper; knows the old channel will return." },
  { person: "Lin", chapter: 3, text: "Adds a new entry to the log as the villages prepare to water their gardens." },
  { person: "Mei", chapter: 3, text: "Uses the lantern and hillside mirrors to signal the villages to open their water gates." },
];

export function demoFacts(person: DemoPerson, chapter: number) {
  return facts.filter(fact => fact.person === person && fact.chapter <= chapter);
}
