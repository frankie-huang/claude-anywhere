"""stores —— JSON 文件持久化的单例 store 集合

各 store 继承 `stores.json_store.JsonStore`（统一 _load/_save + per-subclass 单例
+ 文件 I/O 机械细节），采用「全量加载 + 全量保存」的访问模型，子类只声明
STORE_NAME / LOG_TAG 即可。

换存储介质（SQLite/Redis 等）：本层只在 JsonStore 的 _load/_save + __init__ 这条
路径耦合「文件」。新建一个同接口的基类（实现 _load/_save，复用单例 / _file_lock /
_post_init，把 STORE_NAME 当表名或 key 名），各 store 把继承的 JsonStore 换成它即可
——前提是沿用「全量加载 + 全量保存」。

注意：全量读改写只在「单写者」下安全。当前 callback 后端是单进程，OK；若换介质是
为多进程 / 多实例共享存储，全量保存有跨进程丢更新竞态（_file_lock 只是进程内锁），
那需改成按 key 操作 + DB 级原子性，而非换基类。
"""
