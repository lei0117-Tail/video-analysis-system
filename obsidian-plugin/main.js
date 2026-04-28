const { Plugin, Notice, TFile } = require('obsidian');

class AIVideoNotesPlugin extends Plugin {
  async onload() {
    console.log('加载AI Video Notes插件');

    // 注册命令
    this.addCommand({
      id: 'import-video-notes',
      name: '导入视频分析笔记',
      callback: () => this.importVideoNotes()
    });

    // 添加功能区图标
    const ribbonIconEl = this.addRibbonIcon('video', 'AI视频笔记', (evt) => {
      new Notice('AI视频笔记插件已激活');
    });

    // 监听文件创建事件
    this.registerEvent(
      this.app.vault.on('create', (file) => {
        if (file instanceof TFile && file.path.includes('AI视频分析笔记')) {
          console.log('新视频分析笔记创建:', file.path);
        }
      })
    );
  }

  async onunload() {
    console.log('卸载AI Video Notes插件');
  }

  async importVideoNotes() {
    const folder = this.app.vault.getFolderByPath('AI视频分析笔记');
    if (!folder) {
      new Notice('未找到AI视频分析笔记文件夹');
      return;
    }

    const files = folder.children.filter(f => f instanceof TFile && f.extension === 'md');
    new Notice(`找到 ${files.length} 个视频分析笔记`);

    // 这里可以添加更多处理逻辑
  }
}

module.exports = AIVideoNotesPlugin;