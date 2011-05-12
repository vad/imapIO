'IMAP mailbox wrapper'
import re
import os
import gzip
import email
import random
import chardet
import imaplib
import datetime
import mimetypes
import logging; log = logging.getLogger(__name__)
from calendar import timegm
from email.generator import Generator
from email.parser import HeaderParser
from email.utils import mktime_tz, parsedate_tz, formatdate, parseaddr, formataddr, getaddresses
from email.header import decode_header, HeaderParseError


__all__ = ['IMAP4', 'IMAP4_SSL', 'IMAPError', 'Email', 'clean_nickname', 'parse_tags', 'format_tags', 'build_message', 'extract_parts']


pattern_folder = re.compile(r'\((?P<flags>.*?)\) "(?P<delimiter>.*)" (?:\{.*\})?(?P<name>.*)')
pattern_uid = re.compile(r'UID (\d+)')
pattern_whitespace = re.compile(r'\s+')
pattern_domain = re.compile(r'@[^,]+|/[^,]+')


class _IMAPExtension(object):
    'Mixin class that extends the IMAP interface'

    def __str__(self):
        return '%s@%s:%s' % (self.user, self.host, self.port)

    def format_error(self, text, data=None):
        'Format error string'
        return '[%s] %s%s' % (self, text, '\n' + repr(data) if data else '')

    @property
    def folders(self):
        'Parse folder names'
        self.cd()
        folders = []
        r, data = self.list()
        if r != 'OK':
            raise IMAPError(self.format_error('Could not fetch folders', data))
        for item in data:
            if not item:
                continue
            if isinstance(item, tuple):
                item = ' '.join(item)
            folders.append(pattern_folder.match(item).groups()[2].lstrip())
        return folders

    def cd(self, folder=None):
        'Select the specified folder and return message count'
        r, data = self.select() if folder is None else self.select(folder)
        if r != 'OK':
            log.warn(self.format_error('[%s] Could not select folder' % folder, data))
            return 0
        return int(data[0])

    def walk(self, includes=None, excludes=None, criterion=u''):
        """
        Generate messages from folders matching includes/excludes and messages matching criterion.
        Without arguments, it will walk all folders.
        With includes, it will walk only folders with matching names or matching parent names.
        With excludes, it will skip folders with matching names or matching parent names.
        Please see IMAP specification for more details on search criterion.
        """
        if includes and not hasattr(includes, '__iter__'):
            includes = [includes]
        if excludes and not hasattr(excludes, '__iter__'):
            excludes = [excludes]
        includes = {clean_tag(x) for x in includes} if includes else set()
        excludes = {clean_tag(x) for x in excludes} if excludes else set()
        # Cycle folders in random order
        folders = self.folders
        random.shuffle(folders)
        for folder in folders:
            # Extract tags
            tags = parse_tags(folder)
            # If there are tags to exclude, skip the folder
            if excludes.intersection(tags):
                continue
            # If includes are defined and there are no tags to include, skip the folder
            if includes and not includes.intersection(tags):
                continue
            # Cycle messages in random order
            messageCount = self.cd(folder)
            if criterion:
                criterion = '(%s)' % criterion.encode('utf-8')
                try:
                    r, data = self.search('utf-8', criterion)
                except self.error, error:
                    log.warn(format_error("Could not search mailbox with criterion='%s'" % criterion, error))
                    continue
                if r != 'OK':
                    log.warn(format_error("Could not search mailbox with criterion='%s'" % criterion, data))
                    continue
                messageIndices = map(int, data[0].split())
            else:
                messageIndices = range(1, messageCount + 1)
            random.shuffle(messageIndices)
            for messageIndex in messageIndices:
                # Prepare error template
                format_error = lambda text, data: self.format_error('[INDEX=%s %s] %s' % (messageIndex, format_tags(tags), text), data)
                # Load message header
                try:
                    r, data = self.fetch(messageIndex, '(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO CC BCC DATE)] UID)')
                # If the connection died, log it and quit
                except imaplib.IMAP4.abort, error:
                    raise IMAPError(format_error('Connection failed while fetching message header', error))
                # If we could not fetch the header, log it and move on
                if r != 'OK':
                    log.warn(format_error('Could not peek at message header', data))
                    continue
                # Extract uid
                try:
                    match = pattern_uid.search(data[1])
                    if not match:
                        match = pattern_uid.search(data[0][0])
                        if not match:
                            raise IndexError
                # If we could not extract the uid, log it and move on
                except IndexError:
                    log.warn(format_error('Could not extract uid', data))
                    continue
                # Yield
                yield Email(self, int(match.group(1)), tags, data[0][1])

    def revive(self, targetFolder, message):
        'Upload the message to the targetFolder of the mail server'
        # Find the folder on the mail server
        for folder in self.folders:
            # If the folder exists, exit loop
            if folder.lower() == targetFolder.lower():
                break
        # If the folder does not exist, create it
        else:
            self.create(targetFolder)
            folder = targetFolder
        # Append
        r, data = self.append(folder, '', mktime_tz(parsedate_tz(message['Date'])), message.as_string(False))
        if r != 'OK':
            raise IMAPError(self.format_error('Could not revive message', data))
        return data[0]


class IMAP4(_IMAPExtension, imaplib.IMAP4):
    'Extended IMAP4 client class'

    def __init__(self, host='', port=imaplib.IMAP4_PORT):
        self.host = host
        self.port = port
        imaplib.IMAP4.__init__(self, host, port)
        _IMAPExtension.__init__(self)

    def login(self, user, password):
        self.user = user
        imaplib.IMAP4.login(self, user, password)

    @classmethod
    def connect(cls, host='', port=imaplib.IMAP4_PORT, user='', password=''):
        'Connect, login, return class instance'
        try:
            server = cls(host, port)
            server.login(user, password)
        except Exception, error:
            server = _IMAPExtension()
            server.host = host
            server.port = port
            server.user = user
            raise IMAPError(server.format_error('Could not connect to server', error))
        return server


class IMAP4_SSL(_IMAPExtension, imaplib.IMAP4_SSL):
    'Extended IMAP4 client class over SSL connection'

    def __init__(self, host='', port=imaplib.IMAP4_SSL_PORT, keyfile=None, certfile=None):
        self.host = host
        self.port = port
        imaplib.IMAP4_SSL.__init__(self, host, port, keyfile, certfile)
        _IMAPExtension.__init__(self)

    def login(self, user, password):
        self.user = user
        imaplib.IMAP4_SSL.login(self, user, password)

    @classmethod
    def connect(cls, host='', port=imaplib.IMAP4_SSL_PORT, user='', password='', keyfile=None, certfile=None):
        'Connect, login, return class instance'
        try:
            server = cls(host, port, keyfile, certfile)
            server.login(user, password)
        except Exception, error:
            server = _IMAPExtension()
            server.host = host
            server.port = port
            server.user = user
            raise IMAPError(server.format_error('Could not connect to server', error))
        return server


class IMAPError(Exception):
    'IMAP error'
    pass


class Email(object):
    'Convenience class representing an email from an IMAP mailbox'

    def __init__(self, server, uid, tags, header):
        self.server = server
        self.uid = uid
        self.tags = tags
        # Parse header
        valueByKey = HeaderParser().parsestr(header)
        def getWhom(field):
            return ', '.join(formataddr((self._decode(x), y)) for x, y in getaddresses(valueByKey.get_all(field, [])))
        # Extract fields
        if 'Date' in valueByKey:
            timePack = parsedate_tz(valueByKey['Date'])
            self.whenUTC = datetime.datetime.utcfromtimestamp(timegm(timePack) if timePack[-1] is None else mktime_tz(timePack)) if timePack else None
        else:
            self.whenUTC = None
        self.subject = self._decode(valueByKey.get('Subject', ''))
        self.fromWhom = getWhom('From')
        self.toWhom = getWhom('To')
        self.ccWhom = getWhom('CC')
        self.bccWhom = getWhom('BCC')

    def _decode(self, text):
        'Decode text into utf-8'
        try:
            packs = decode_header(text)
        except HeaderParseError:
            try:
                packs = decode_header(text.replace('?==?', '?= =?'))
            except HeaderParseError:
                log.warn(self.format_error('Could not decode header', text))
                packs = [(text, 'utf-8')]
        string = ''.join(part.decode(encoding or 'utf-8', errors='ignore') for part, encoding in packs)
        return pattern_whitespace.sub(' ', string.strip())

    def format_error(self, text, data=None):
        'Format error string'
        return self.server.format_error('[UID=%s %s] %s' % (self.uid, format_tags(self.tags), text), data)

    @property
    def flags(self):
        'Get flags'
        r, data = self.server.uid('fetch', self.uid, '(FLAGS)')
        if r != 'OK':
            raise IMAPError(self.format_error('Could not get flags', data))
        string = data[0]
        return imaplib.ParseFlags(string) if data else ()

    @flags.setter
    def flags(self, flags):
        'Set flags'
        if not hasattr(flags, '__iter__'):
            flags = [flags]
        r, data = self.server.uid('store', self.uid, 'FLAGS', '(%s)' % ' '.join(flags))
        if r != 'OK':
            raise IMAPError(self.format_error('Could not set flags', data))

    def set_flag(self, flag, on=True):
        'Set flag on or off'
        operator = '+' if on else '-'
        r, data = self.server.uid('store', self.uid, operator + 'FLAGS', '(%s)' % flag)
        if r != 'OK':
            raise IMAPError(self.format_error('Could not flag email', data))
        return self

    @property
    def seen(self):
        'Return True if email is marked as seen'
        return True if r'\Seen' in self.flags else False

    @seen.setter
    def seen(self, on=True):
        'Flag the email as seen or not'
        return self.set_flag(r'\Seen', on)

    @property
    def deleted(self):
        'Return True if email is marked as deleted'
        return True if r'\Deleted' in self.flags else False

    @deleted.setter
    def deleted(self, on=True):
        'Flag the email as deleted or not'
        return self.set_flag(r'\Deleted', on)

    def save(self, targetPath=None):
        """
        Save email to the hard drive and return a list of parts by index, type, name.
        Compress the file if the filename ends with .gz
        Return partPacks if targetPath=None.
        """
        try:
            # Get
            flags = self.flags
            # Load message
            r, data = self.server.uid('fetch', self.uid, '(RFC822)')
            if r != 'OK':
                raise IMAPError(self.format_error('Could not fetch message body', data))
            message = email.message_from_string(data[0][1])
            # Restore
            self.flags = flags
        except imaplib.IMAP4.abort, error:
            raise IMAPError(self.format_error('Connection failed while fetching message body', error))
        # Save
        if targetPath:
            Generator((gzip.open if targetPath.endswith('.gz') else open)(targetPath, 'wb')).flatten(message)
        # Gather partPacks
        partPacks = []
        for partIndex, part in enumerate(message.walk()):
            if 'multipart' == part.get_content_maintype():
                continue
            partPacks.append((partIndex, part.get_filename(), part.get_content_type()))
        return partPacks

    def __getitem__(self, key):
        return getattr(self, key)

    def __setitem__(self, key, value):
        return setattr(self, key, value)


def clean_nickname(text):
    'Extract nickname from email address'
    nickname, address = parseaddr(text)
    if not nickname:
        nickname = address
    return pattern_whitespace.sub(' ', pattern_domain.sub('', nickname).replace('.', ' ').replace('_', ' ')).strip('" ').title()


def parse_tags(text):
    return [clean_tag(x) for x in text.replace('&-', '&').split('\\')]


def format_tags(tags, separator=' '):
    'Format tags'
    return separator.join(x.encode('utf-8') for x in tags)


def clean_tag(text):
    'Convert to lowercase unicode, strip quotation marks, compact whitespace'
    return pattern_whitespace.sub(' ', text.lower().strip('" ')).decode('utf-8')


def build_message(whenUTC=None, subject='', fromWhom='', toWhom='', ccWhom='', bccWhom='', bodyText='', bodyHTML='', attachmentPaths=None):
    'Build MIME message'
    mimeText = email.MIMEText.MIMEText(bodyText.encode('utf-8'), _charset='utf-8')
    mimeHTML = email.MIMEText.MIMEText(bodyHTML.encode('utf-8'), 'html')
    if attachmentPaths:
        message = email.MIMEMultipart.MIMEMultipart()
        if bodyText and bodyHTML:
            messageAlternative = email.MIMEMultipart.MIMEMultipart('alternative')
            messageAlternative.attach(mimeText)
            messageAlternative.attach(mimeHTML)
            message.attach(messageAlternative)
        elif bodyText:
            message.attach(mimeText)
        elif bodyHTML:
            message.attach(mimeHTML)
        for attachmentPath in attachmentPaths:
            attachmentName = os.path.basename(attachmentPath)
            contentType, contentEncoding = mimetypes.guess_type(attachmentName)
            # If we could not guess the type or the file is compressed,
            if contentType is None or contentEncoding is not None:
                contentType = 'application/octet-stream'
            mainType, subType = contentType.split('/', 1)
            payload = open(attachmentPath, 'rb').read()
            if mainType == 'text':
                part = email.MIMEText.MIMEText(payload, _subtype=subType, _charset=chardet.detect(payload)['encoding'])
            elif mainType == 'image':
                part = email.MIMEImage.MIMEImage(payload, _subtype=subType)
            elif mainType == 'audio':
                part = email.MIMEAudio.MIMEAudio(payload, _subtype=subType)
            else:
                part = email.MIMEBase.MIMEBase(mainType, subType)
                part.set_payload(payload)
                email.Encoders.encode_base64(part)
            part.add_header('Content-Disposition', 'attachment', filename=attachmentName)
            message.attach(part)
    elif bodyText and bodyHTML:
        message = email.MIMEMultipart.MIMEMultipart('alternative')
        message.attach(mimeText)
        message.attach(mimeHTML)
    elif bodyText:
        message = mimeText
    elif bodyHTML:
        message = mimeHTML
    message['Date'] = formatdate(timegm((whenUTC or datetime.datetime.utcnow()).timetuple()))
    message['Subject'] = subject.encode('utf-8')
    message['From'] = fromWhom.encode('utf-8')
    message['To'] = toWhom.encode('utf-8')
    message['CC'] = ccWhom.encode('utf-8')
    message['BCC'] = bccWhom.encode('utf-8')
    return message


def extract_parts(sourcePath, partIndices=None, peek=False, applyCharset=True):
    """
    Get attachment from email sourcePath.
    Set partIndices=None to see all parts.
    Set peek=True to omit the payload.
    Set applyCharset=True to decode the payload into unicode.
    """
    if not hasattr(partIndices, '__iter__'):
        partIndices = [partIndices] if partIndices else []
    message = email.message_from_file(gzip.open(sourcePath, 'rb') if sourcePath.endswith('.gz') else open(sourcePath, 'rb'))
    partPacks = []
    for partIndex, part in enumerate(message.walk()):
        if 'multipart' == part.get_content_maintype():
            continue
        if partIndices and partIndex not in partIndices:
            continue
        partPack = partIndex, part.get_filename(), part.get_content_type()
        if not peek:
            payload = part.get_payload(decode=True)
            if applyCharset:
                charset = part.get_content_charset() or part.get_charset() or chardet.detect(payload)['encoding']
                payload = payload.decode(charset, errors='ignore')
            partPack += (payload,)
        partPacks.append(partPack)
    return partPacks
