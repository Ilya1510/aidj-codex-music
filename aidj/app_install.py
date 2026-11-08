"""Public, private-chat installation requests always issue listener credentials."""
from .android_release import version,connection_url
from .web_auth import browser_code
from .webapp import public_url


class AppInstallUI:
    def __init__(self,db,bot,settings):self.db,self.bot,self.settings=db,bot,settings

    def menu(self,chat):
        self.bot.message(chat,'AI DJ для прослушивания: музыка, скачивание и DJ. Общие плейлисты защищены от изменений. Выбери устройство:',
                         [[{'text':'Android · APK','callback_data':'appinstall:android'},
                           {'text':'iPhone · веб-плеер','callback_data':'appinstall:ios'}]])

    def install(self,chat,user,platform):
        origin=public_url(self.settings)
        if not origin:self.bot.message(chat,'Плеер пока недоступен. Попробуй чуть позже.');return
        if platform=='ios':
            url=origin+'/#login='+browser_code(self.db,role='listener',user_id=user)
            self.bot.message(chat,'Открой ссылку в Safari. Затем «Поделиться» → «На экран Домой». Это веб-приложение для iPhone. Ссылка входа действует пять минут и используется один раз. Веб-DJ работает с открытым плеером; фоновый DJ доступен в Android-приложении. Общие плейлисты доступны для прослушивания.',
                             [[{'text':'Открыть плеер для iPhone','url':url}]])
            return
        info=version(self.settings,origin)
        if not info:self.bot.message(chat,'Android-приложение ещё собирается. Попробуй чуть позже.');return
        file=self.settings.state/'android-release/releases'/str(info['versionCode'])/'AI-DJ.apk'
        if not file.is_file():self.bot.message(chat,'Установочный файл временно недоступен. Попробуй чуть позже.');return
        url=connection_url(self.db,origin,role='listener',user_id=user)
        self.bot.document(chat,file,'AI DJ '+info['versionName']+' · слушатель\nУстанови APK, затем нажми «Подключить плеер». Ссылка действует пять минут; новую можно получить командой /apk.',
                          [[{'text':'Подключить плеер','url':url}]])

    def message(self,update,is_owner):
        message=update.get('message') or {};chat=message.get('chat') or {};user=message.get('from') or {}
        if chat.get('type')!='private' or type(user.get('id')) is not int or user.get('is_bot'):return False
        text=(message.get('text') or '').strip();command=text.split(' ',1)[0].split('@',1)[0].lower()
        start=text.partition(' ')[2].strip().lower() if command=='/start' else ''
        if command in ('/apk','apk','апк') or start=='apk':self.menu(chat['id']);return True
        if command=='/ios':self.install(chat['id'],user['id'],'ios');return True
        if not is_owner and (command=='/android' or start=='android'):self.install(chat['id'],user['id'],'android');return True
        if not is_owner and command=='/app':self.menu(chat['id']);return True
        return False

    def callback(self,update):
        query=update.get('callback_query') or {};data=query.get('data','')
        if data not in ('appinstall:android','appinstall:ios'):return False
        chat=(query.get('message') or {}).get('chat') or {};user=query.get('from') or {}
        if chat.get('type')!='private' or type(user.get('id')) is not int or user.get('is_bot'):return True
        self.bot.call('answerCallbackQuery',{'callback_query_id':query['id']})
        self.install(chat['id'],user['id'],data.split(':')[1]);return True
