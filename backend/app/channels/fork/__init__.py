"""Fork 专属的 IM 渠道扩展（WillTheFabulous/deer-flow）。

fork 在上游 channels 之上新增的功能都集中在本包里，同步上游时只需复核下面几个挂接点：

- ``store.py``：``ChannelStore`` 继承 :class:`store_ext.ChannelStoreExtensionsMixin`，
  且 ``set_thread_id`` 改为合并写，保证新建线程时扩展字段不丢。
- ``commands.py``：登记 ``/repo``、``/sessions``、``/model`` 命令。
- ``service.py``：构造 :class:`manager.ForkChannelManager`，``feishu`` 渠道加载
  :class:`feishu.ForkFeishuChannel`。
- ``manager.py``（上游）：``ChannelManager._make_stream_observer`` 钩子，
  由 :class:`progress.StreamProgressObserver` 实现流式进度。
- ``feishu.py``（上游）：``FeishuChannel._event_handler_builder``，fork 借此注册
  卡片回调与机器人菜单事件。

本模块保持零导入：``store.py`` 在加载时就会导入 ``store_ext``。
"""
